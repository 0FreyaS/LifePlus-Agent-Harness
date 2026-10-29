"""Business goal completion via actual HTTP handlers, model, graph, MCP and storage."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx

from .run import TZ, expected_slots, normalize, percentile, wilson


def check_goal(goal, leaf, trace, state, all_traces, anchor):
    """The oracle never trusts completed alone or asks the serving LLM to grade itself."""
    failures = []
    routes = [event for event in trace if event["type"] == "route.completed"]
    domain = routes[-1].get("domain") if routes else leaf.get("domain")
    if domain != goal["domain"]:
        failures.append("wrong_domain")
    if leaf["status"] != "completed":
        failures.append("leaf_not_completed")
    if not leaf.get("answer", "").strip():
        failures.append("empty_answer")
    if goal.get("answer_contains") and goal["answer_contains"] not in leaf.get("answer", ""):
        failures.append("answer_missing_requested_entity")
    tool = goal.get("tool")
    if not tool:
        if any(event["type"] == "tool.started" for event in trace):
            failures.append("unexpected_tool_for_chat")
        return failures
    completed = [event for event in trace if event["type"] == "tool.completed" and event.get("tool_name") == tool]
    if not completed:
        return failures + ["missing_successful_tool_receipt:" + tool]
    event = completed[-1]
    result = event.get("result", {})
    if result.get("simulated") or result.get("error") or result.get("ok") is not True:
        failures.append("invalid_or_simulated_receipt")
    started = [item for item in trace if item["type"] == "tool.started" and item.get("job_id") == event.get("job_id")]
    arguments = started[-1].get("arguments", {}) if started else {}
    for key, value in expected_slots(goal.get("arguments", {}), anchor).items():
        if normalize(arguments.get(key), key) != normalize(value, key):
            failures.append("argument_mismatch:" + key)
    if goal.get("origin_contains") and goal["origin_contains"] not in arguments.get("origin", ""):
        failures.append("origin_not_preserved")
    if tool == "maps_weather":
        if not result.get("day_weather") or not result.get("source"):
            failures.append("weather_evidence_missing")
        target = expected_slots(goal["arguments"], anchor)
        if normalize(result.get("city", ""), "city") != normalize(target["city"], "city"):
            failures.append("wrong_weather_city")
        if result.get("date") != target["forecast_date"]:
            failures.append("wrong_weather_date")
    if tool in {"search_music", "maps_local_search"}:
        items = result.get("items") or []
        if not items:
            failures.append("no_matching_result")
        index = int(state.get("slots", {}).get("selection", 1)) - 1
        if not 0 <= index < len(items):
            failures.append("invalid_selected_result")
        elif tool == "search_music" and not items[index].get("url"):
            failures.append("selected_song_has_no_link")
        elif goal.get("detail"):
            poi_id = items[index].get("id")
            details = [item for item in trace if item["type"] == "tool.completed" and item.get("tool_name") == "maps_place_detail"]
            if not any(item.get("result", {}).get("item", {}).get("id") == poi_id for item in details):
                failures.append("selected_poi_detail_not_loaded")
    if tool == "maps_route":
        if result.get("distance_m", 0) <= 0 or result.get("duration_s", 0) <= 0 or not result.get("navigation_url"):
            failures.append("route_evidence_missing")
        items = state.get("tool_result", {}).get("items") or []
        index = int(state.get("slots", {}).get("selection", 1)) - 1
        if not (0 <= index < len(items) and normalize(arguments.get("destination")) == normalize(items[index].get("location"))):
            failures.append("route_not_bound_to_selected_poi")
    if tool in {"save_itinerary", "create_reminder"}:
        expected_status = "saved" if tool == "save_itinerary" else "scheduled"
        resource_key = "item_id" if tool == "save_itinerary" else "reminder_id"
        if result.get("status") != expected_status or not result.get(resource_key):
            failures.append("write_receipt_missing")
        approvals = [item for item in trace if item["type"] == "user.resumed" and item.get("action") == "approve"]
        if not approvals or approvals[-1]["seq"] >= event["seq"]:
            failures.append("write_without_prior_approval")
        if len(completed) != 1:
            failures.append("duplicate_write")
        if goal.get("receipt_from"):
            upstream = [item["result"] for events in all_traces for item in events
                        if item["type"] == "tool.completed" and item.get("tool_name") == goal["receipt_from"]]
            details = result.get("details", {})
            if not upstream or not isinstance(details, dict) or any(details.get(key) != upstream[0].get(key) for key in ("city", "date", "day_weather")):
                failures.append("upstream_evidence_not_saved")
    return failures


def report_rows(rows, cases, metadata):
    groups = {}
    for group in sorted({case["group"] for case in cases}):
        subset = [row for row in rows if row["group"] == group]
        num = sum(row["completed"] for row in subset)
        statuses_completed = sum(row.get("all_roots_completed", False) for row in subset)
        false_completions = sum(row.get("all_roots_completed", False) and not row["completed"] for row in subset)
        groups[group] = {"tested": len(subset), "planned": sum(case["group"] == group for case in cases),
            "completed": num, "completion_rate": num / len(subset) if subset else None,
            "wilson95": wilson(num, len(subset)), "goals_completed": sum(row.get("goals_completed", 0) for row in subset),
            "goals_total": sum(row["goals_total"] for row in subset),
            "structured_false_completion": {"numerator": false_completions, "denominator": statuses_completed},
            "p50_ms": percentile([row["elapsed_ms"] for row in subset], .5),
            "p95_ms": percentile([row["elapsed_ms"] for row in subset], .95),
            "failures": [row["id"] for row in subset if not row["completed"]]}
    return {**metadata, "tested": len(rows), "completed": sum(row["completed"] for row in rows), "groups": groups}


def write_report(output, rows, cases, metadata):
    report = report_rows(rows, cases, metadata)
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# LifePlus 真实任务完成率", "", f"模型：`{metadata['model']}`；并发：{metadata['concurrency']}；开始：{metadata['started_at']}",
             "", "| 场景 | 已测/计划 | 全目标完成 | 完成率 | 分目标完成 | P95 |", "|---|---:|---:|---:|---:|---:|"]
    for name, group in report["groups"].items():
        rate = f"{group['completion_rate']:.1%}" if group["tested"] else "N/A"
        lines.append(f"| {name} | {group['tested']}/{group['planned']} | {group['completed']} | {rate} | {group['goals_completed']}/{group['goals_total']} | {group['p95_ms']}ms |")
    lines.extend(["", "## 失败明细", ""])
    for row in rows:
        if not row["completed"]:
            lines.append(f"- `{row['id']}`：{'; '.join(row.get('failures', [])) or row.get('error', 'unknown')}")
    lines.extend(["", "## 口径", "",
        "- 分母包含所有已提交样本，包括超时、未完成、工具故障及模型错误；部分成功不算全目标完成。",
        "- 每条使用独立用户与会话，经过真实 HTTP handler（ASGI transport）、模型、LangGraph、MCP 和存储，不注入模型或工具回执。没有实测 TCP/浏览器传输开销。",
        "- 用户模拟器只提供数据集中约定的信息、选择实际展示的候选、确认数据集中已请求的写操作；不为模型补写计划或槽位，不修改任务状态。",
        "- 完成要求：所有根任务与子任务结束、全部目标的领域/参数/成功回执满足断言、候选与路线绑定正确、写操作先确认且可在数据库中读取。",
        "- 天气核对城市和日期，不核验天气事实；音乐仅承诺检索及链接，不承诺播放；本地生活仅承诺检索和详情，不承诺实时营业或预订。",
        "- 结构化虚假完成指状态已 completed 但未通过目标证据核对，不等于人工评审的全部自然语言幻觉。",
        "- 时延是从提交到全部目标结束的自动交互会话耗时，包含模型生成、重试、工具和自动选择/澄清，无真实人工思考等待；超时不剔除。",
        "- 小规模人工设计场景不能作为线上 SLA。全量 Trace、Checkpoints 与工具账本保留在隔离 runtime.sqlite3；不改正常用户数据。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


async def run(args):
    # Import here so metric unit tests do not initialize application settings.
    from car_agent.api.app import app, settings
    from car_agent.orchestration import TERMINAL

    dataset = Path(args.dataset)
    cases = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [case for case in cases if case["id"] in wanted]
        if wanted != {case["id"] for case in cases}:
            raise ValueError("Unknown case ID")
    if args.limit_per_group:
        selected, counts = [], Counter()
        for case in cases:
            if counts[case["group"]] < args.limit_per_group:
                selected.append(case)
                counts[case["group"]] += 1
        cases = selected
    output = Path(args.output)
    if output.exists():
        raise ValueError("Use a new run directory; previous evidence must not be overwritten")
    output.mkdir(parents=True)
    anchor = datetime.now(TZ)
    metadata = {"started_at": anchor.isoformat(), "model": settings.model_name,
        "domain_route_provider": settings.domain_route_provider,
        "input_event_provider": settings.input_event_provider,
        "planning_gate_provider": settings.planning_gate_provider,
        "role_models": {role: getattr(settings, role + "_model_name") or settings.model_name for role in ("planner", "intent", "response")},
        "execution_conditions": {name: getattr(settings, name) for name in (
            "model_base_url", "model_max_output_tokens", "model_stream_usage", "model_thinking", "planner_variant", "planner_max_output_tokens",
            "intent_max_output_tokens", "response_max_output_tokens", "runtime_active_seconds", "runtime_model_calls",
            "runtime_tool_calls", "runtime_resumes", "runtime_refreshes", "runtime_retries", "runtime_graph_steps",
            "runtime_planning_seconds", "input_event_timeout_seconds", "tool_queue_workers",
            "tool_queue_timeout_seconds", "tool_queue_max_retries", "memory_history_messages", "memory_history_characters")},
        "jev": {"model": settings.jev_model_name, "timeout_seconds": settings.jev_timeout_seconds,
                "min_confidence": settings.jev_min_confidence, "min_probability": settings.jev_min_probability},
        "concurrency": args.concurrency, "case_budget_seconds": args.case_budget,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "selected_ids": [case["id"] for case in cases], "checkpoint_backend": "sqlite",
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            *Path("src/car_agent").rglob("*.py"), Path(__file__), Path("evaluations/completion_dataset.py")]}}
    (output / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    original = {key: getattr(settings, key) for key in ("database_path", "chroma_path", "checkpoint_postgres_dsn", "redis_url", "mysql_dsn", "app_mode")}
    settings.database_path = str((output / "runtime.sqlite3").resolve())
    settings.chroma_path = str((output / "chroma").resolve())
    settings.checkpoint_postgres_dsn = settings.redis_url = settings.mysql_dsn = None
    settings.app_mode = "local"
    rows = []
    semaphore = asyncio.Semaphore(args.concurrency)
    run_id = uuid.uuid4().hex[:12]
    try:
        async with app.router.lifespan_context(app):
            if not app.state.context.llm or not settings.amap_maps_api_key:
                raise RuntimeError("Real model and map configuration are required")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://completion-eval", timeout=30) as client:
                async def evaluate(case):
                    async with semaphore:
                        started = perf_counter()
                        owner = f"completion-{run_id}-{case['id']}"
                        headers = {"X-Demo-User": owner}
                        root_ids, interactions = [], []
                        row = {"id": case["id"], "group": case["group"], "query": case["query"], "goals_total": len(case["goals"]),
                               "completed": False, "owner": owner, "interactions": interactions, "failures": []}

                        async def request(method, url, **kwargs):
                            result = await client.request(method, url, headers=headers, **kwargs)
                            result.raise_for_status()
                            return result.json()

                        async def workflow():
                            session = (await request("POST", "/api/sessions"))["id"]
                            submitted = await request("POST", f"/api/sessions/{session}/messages", json={"message": case["query"], "mode": "live", "request_id": case["id"]})
                            root_ids.append(submitted["task_id"])
                            policy = case["user_policy"]
                            answered = Counter()
                            compound_sent = False
                            while True:
                                roots = [await request("GET", f"/api/tasks/{tid}") for tid in root_ids]
                                if all(root["status"] in TERMINAL for root in roots):
                                    break
                                for root in roots:
                                    for task in root.get("children") or [root]:
                                        pending = task.get("interrupt")
                                        if task["status"] != "waiting_user" or not pending:
                                            continue
                                        kind, field = pending.get("kind"), pending.get("field")
                                        if len(interactions) >= 14:
                                            raise RuntimeError("interaction_budget_exhausted")
                                        if kind == "clarification":
                                            text = policy.get("answers", {}).get(field)
                                            if text is None:
                                                raise RuntimeError("unexpected_clarification:" + str(field))
                                            answered[field] += 1
                                            if answered[field] > 2:
                                                raise RuntimeError("repeated_clarification:" + str(field))
                                            if policy.get("compound") and policy.get("compound_on_field") == field and not compound_sent:
                                                text += policy.get("compound_separator", "，") + policy["compound"]
                                                compound_sent = True
                                            action, value = "answer", {"text": text}
                                        elif kind == "selection":
                                            candidates = pending.get("candidates") or []
                                            if not candidates:
                                                raise RuntimeError("selection_without_candidates")
                                            action, value = "answer", {"index": min(2, len(candidates))}
                                            if policy.get("selection_text"):
                                                value = {"text": "第二个" if len(candidates) >= 2 else "第一个"}
                                            if policy.get("compound") and not policy.get("compound_on_field") and not compound_sent:
                                                value = {"text": ("第二个" if len(candidates) >= 2 else "第一个") + policy.get("compound_separator", "，") + policy["compound"]}
                                                compound_sent = True
                                        elif kind == "approval":
                                            if not any(goal["tool"] == pending.get("action") for goal in case["goals"]):
                                                raise RuntimeError("unrequested_write_approval")
                                            action, value = "approve", {}
                                        else:
                                            raise RuntimeError("unknown_interrupt:" + str(kind))
                                        if policy.get("reply_entry") == "composer" and action == "answer" and "text" in value:
                                            response = await request("POST", f"/api/sessions/{session}/messages", json={"message": value["text"], "mode": "live"})
                                        else:
                                            response = await request("POST", f"/api/tasks/{task['id']}/resume", json={"interrupt_id": pending["id"], "action": action, "value": value})
                                        interactions.append({"task_id": task["id"], "kind": kind, "field": field, "action": action, "value": value})
                                        if response.get("created_task_id"):
                                            root_ids.append(response["created_task_id"])
                                await asyncio.sleep(.1)
                            leaves = [leaf for root in roots for leaf in (root.get("children") or [root])]
                            traces = [await request("GET", f"/api/tasks/{leaf['id']}/trace") for leaf in leaves]
                            states = []
                            for leaf in leaves:
                                snapshot = await app.state.graph.aget_state({"configurable": {"thread_id": leaf["thread_id"]}})
                                states.append(snapshot.values)
                            row["all_roots_completed"] = all(root["status"] == "completed" for root in roots)
                            row["root_statuses"] = [{"id": root["id"], "status": root["status"]} for root in roots]
                            row["leaf_statuses"] = [{"id": leaf["id"], "status": leaf["status"], "answer": leaf.get("answer")} for leaf in leaves]
                            matrix = [[check_goal(goal, leaf, trace, state, traces, anchor) for leaf, trace, state in zip(leaves, traces, states)] for goal in case["goals"]]
                            assignments = itertools.permutations(range(max(len(leaves), len(case["goals"]))), len(case["goals"]))
                            choices = min(assignments, key=lambda indexes: sum(len(matrix[i][j]) if j < len(leaves) else 100 for i, j in enumerate(indexes)))
                            checks = [{"goal": goal, "leaf_id": leaves[j]["id"] if j < len(leaves) else None,
                                "failures": matrix[i][j] if j < len(leaves) else ["missing_task"]}
                                for i, (goal, j) in enumerate(zip(case["goals"], choices))]
                            for checked in checks:
                                goal = checked["goal"]
                                if checked["leaf_id"] and goal["tool"] in {"save_itinerary", "create_reminder"}:
                                    trace = traces[next(i for i, leaf in enumerate(leaves) if leaf["id"] == checked["leaf_id"])]
                                    receipts = [event["result"] for event in trace if event["type"] == "tool.completed" and event.get("tool_name") == goal["tool"]]
                                    if receipts:
                                        receipt = receipts[-1]
                                        table, resource = ("itinerary_items", receipt.get("item_id")) if goal["tool"] == "save_itinerary" else ("reminders", receipt.get("reminder_id"))
                                        persisted = await app.state.db.fetchone(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (resource, owner))
                                        if not persisted:
                                            checked["failures"].append("write_not_persisted")
                            row["goals_completed"] = sum(not checked["failures"] for checked in checks)
                            row["goal_checks"] = checks
                            row["failures"] = [f"goal{index+1}:{failure}" for index, checked in enumerate(checks) for failure in checked["failures"]]
                            if len(leaves) != len(case["goals"]):
                                row["failures"].append("unexpected_task_count")
                            for field in policy.get("require_fields", []):
                                if not any(item["kind"] == "clarification" and item["field"] == field for item in interactions):
                                    row["failures"].append("missing_required_clarification:" + field)
                            if policy.get("require_compound") and not compound_sent:
                                row["failures"].append("compound_reply_not_exercised")
                            row["completed"] = row["all_roots_completed"] and not row["failures"]

                        try:
                            await asyncio.wait_for(workflow(), timeout=args.case_budget)
                        except Exception as exc:
                            error = str(exc).replace(settings.model_api_key or "__unset__", "[REDACTED]")
                            row.update(error_type=type(exc).__name__, error=error[:1200], completed=False)
                            row["failures"].append(type(exc).__name__ + ":" + error[:300])
                        finally:
                            row["task_ids"] = list(root_ids)
                            row["elapsed_ms"] = round((perf_counter() - started) * 1000)
                            row["trace_files"] = []
                            for root_id in root_ids:
                                task = await request("GET", f"/api/tasks/{root_id}")
                                if task["status"] not in TERMINAL:
                                    await request("POST", f"/api/tasks/{root_id}/cancel")
                                for leaf in task.get("children") or [task]:
                                    events = await request("GET", f"/api/tasks/{leaf['id']}/trace")
                                    path = output / "traces" / f"{case['id']}-{leaf['id']}.json"
                                    path.parent.mkdir(exist_ok=True)
                                    path.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
                                    row["trace_files"].append(str(path))
                            rows.append(row)
                            with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
                                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                            write_report(output, rows, cases, metadata)
                            print(json.dumps({"id": row["id"], "completed": row["completed"], "goals_completed": row.get("goals_completed", 0),
                                "goals_total": row["goals_total"], "elapsed_ms": row["elapsed_ms"], "failures": row["failures"]}, ensure_ascii=False), flush=True)
                await asyncio.gather(*(evaluate(case) for case in cases))
    finally:
        for key, value in original.items():
            setattr(settings, key, value)
    result = write_report(output, rows, cases, metadata)
    print(json.dumps({"output": str(output), "tested": result["tested"], "completed": result["completed"]}))
    return result["completed"] == len(cases) and len(rows) == len(cases)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="evaluations/data/completion-v1.jsonl")
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--case-budget", type=float, default=210)
    parser.add_argument("--limit-per-group", type=int)
    parser.add_argument("--ids")
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4 or args.case_budget <= 0:
        parser.error("concurrency must be 1..4 and case budget must be positive")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
