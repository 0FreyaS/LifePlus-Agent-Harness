"""Explain task latency without pretending parallel work sums are a critical path."""
from __future__ import annotations

from .freshness import parse_time, utcnow


def summarize(tasks: list[dict], events: list[dict], counters: list[dict]) -> dict:
    unique = {}
    for event in events:
        source = event.get("child_task_id") or event["task_id"]
        unique[(source, event.get("child_seq", event["seq"]))] = {**event, "source_task": source}
    events = list(unique.values())
    work = {"model_ms": 0, "tool_dispatch_ms": 0, "ready_queue_ms": 0, "semaphore_ms": 0,
            "retry_backoff_scheduled_ms": 0, "model_attempts": 0, "tool_attempts": 0,
            "input_tokens": 0, "output_tokens": 0, "usage_known_attempts": 0,
            "tool_timing_known_attempts": 0}
    model_stages = {}
    model_spans, tool_spans = set(), set()
    for event in events:
        kind = event["type"].removeprefix("subtask.")
        if kind in {"model.attempt.completed", "model.attempt.failed"} and event.get("span_id") not in model_spans:
            model_spans.add(event.get("span_id"))
            work["model_attempts"] += 1
            work["model_ms"] += event.get("elapsed_ms", 0)
            stage = event.get("stage", "unknown")
            stage_info = model_stages.setdefault(stage, {"attempts": 0, "elapsed_ms": 0, "first_deltas_ms": []})
            stage_info["attempts"] += 1
            stage_info["elapsed_ms"] += event.get("elapsed_ms", 0)
            if event.get("first_delta_ms") is not None:
                stage_info["first_deltas_ms"].append(event["first_delta_ms"])
            metadata = event.get("usage") or {}
            if metadata:
                work["usage_known_attempts"] += 1
                work["input_tokens"] += metadata.get("input_tokens", 0)
                work["output_tokens"] += metadata.get("output_tokens", 0)
        key = (event.get("job_id"), event.get("attempt"))
        if kind == "tool.started":
            work["ready_queue_ms"] += event.get("ready_queue_ms", 0)
            work["semaphore_ms"] += event.get("semaphore_wait_ms", 0)
        if kind in {"tool.completed", "tool.clarification_required", "tool.attempt.failed", "tool.attempt.cancelled"} and key not in tool_spans:
            tool_spans.add(key)
            work["tool_attempts"] += 1
            work["tool_dispatch_ms"] += event.get("dispatch_ms", 0)
            if "dispatch_ms" in event:
                work["tool_timing_known_attempts"] += 1
        if kind == "tool.retry_scheduled":
            work["retry_backoff_scheduled_ms"] += round(event.get("delay_seconds", 0) * 1000)
    per_task = []
    accounted = {row["task_id"]: row for row in counters}
    for task in tasks:
        waiting_start, waiting_ms = None, 0
        for event in sorted((e for e in events if e["source_task"] == task["id"]), key=lambda e: e.get("child_seq", e["seq"])):
            kind = event["type"].removeprefix("subtask.")
            timestamp = parse_time(event["created_at"])
            if kind == "interrupt.required" and waiting_start is None:
                waiting_start = timestamp
            elif waiting_start is not None and kind in {"user.resumed", "task.refresh_requested", "task.cancelled", "task.failed"}:
                waiting_ms += max(0, round((timestamp - waiting_start).total_seconds() * 1000))
                waiting_start = None
        if waiting_start:
            waiting_ms += max(0, round((utcnow() - waiting_start).total_seconds() * 1000))
        per_task.append({"task_id": task["id"], "status": task["status"], "human_wait_ms": waiting_ms,
                         "accounted_usage": accounted.get(task["id"], {})})
    root = tasks[0]
    end = utcnow()
    if root["status"] in {"completed", "failed", "partial", "cancelled"}:
        endings = [parse_time(event["created_at"]) for event in events if event["source_task"] == root["id"]
                   and (event["type"] == "task." + root["status"]
                        or (event["type"] == "group.updated" and event.get("status") == root["status"]))]
        end = max(endings) if endings else parse_time(root.get("updated_at"))
    start = parse_time(root["created_at"])
    return {"wall_ms": max(0, round((end - start).total_seconds() * 1000)), "work": work,
            "model_stages": model_stages,
            "per_task": per_task, "aggregation": "attempt_work_sums_not_critical_path",
            "limitations": ["并行分支耗时之和不是端到端时延", "模型调用耗时包含网络和供应商等待，不等于GPU推理时间",
                            "缺少usage的尝试不计作零Token", "旧Trace缺少时序字段不等于调用耗时为零",
                            "已安排退避不等于实际完整等待，活动耗时在阶段结束时结算"]}
