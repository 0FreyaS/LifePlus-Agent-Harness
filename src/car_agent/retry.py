"""User-triggered model recovery without replaying completed business actions."""
from __future__ import annotations

import json

from fastapi import HTTPException
from .freshness import lifecycle_lock
from .runtime_limits import RuntimeLimitExceeded, consume


RECOVERABLE = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError", "OutputParserException"}


async def failure_kind(db, task_id: str) -> str | None:
    event = await db.fetchone("SELECT data_json FROM events WHERE task_id=? AND type='task.failed' ORDER BY seq DESC LIMIT 1", (task_id,))
    if not event:
        return None
    error = str(json.loads(event["data_json"]).get("error", ""))
    kind = error.split(":", 1)[0].strip()
    kind = {"OpenAIConnectionError": "APIConnectionError", "OpenAITimeoutError": "APITimeoutError",
            "OpenAIRateLimitError": "RateLimitError", "OpenAIInternalServerError": "InternalServerError"}.get(kind, kind)
    return kind if kind in RECOVERABLE else None


async def can_retry(db, task: dict, children: list[dict], max_retries: int = 3) -> bool:
    if task["status"] not in {"failed", "partial"}:
        return False
    counter = await db.fetchone("SELECT retries FROM runtime_usage WHERE task_id=?", (task.get("parent_task_id") or task["id"],))
    if counter and counter["retries"] >= max_retries:
        return False
    for row in children or [task]:
        if row["status"] == "failed" and await failure_kind(db, row["id"]):
            return True
    return False


async def retry_failed(app, task_id: str, owner: str) -> dict:
    db, coordinator = app.state.db, app.state.coordinator
    initial = await db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not initial:
        raise HTTPException(404, "任务不存在")
    root_id = initial.get("parent_task_id") or task_id
    async with lifecycle_lock(app, initial):
        root = await db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (root_id, owner))
        task = await db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
        if not root or not task or task["status"] not in {"failed", "partial"} or root["status"] in {"cancelled", "completed"}:
            raise HTTPException(409, "任务当前不能重试；已完成或已取消的任务不会重新执行")
        if task["mode"] == "live" and not app.state.context.llm:
            raise HTTPException(503, "请先恢复真实模型配置")
        children = await db.fetchall("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY rowid", (root_id,))
        candidates = children if children and task_id == root_id else [task]
        targets = []
        for row in candidates:
            kind = await failure_kind(db, row["id"]) if row["status"] == "failed" else None
            if not kind:
                continue
            snapshot = await app.state.graph.aget_state({"configurable": {"thread_id": row["thread_id"]}})
            checkpoint = bool(snapshot.next)
            if not checkpoint and (row.get("parent_task_id") or snapshot.values):
                continue
            targets.append((row, kind, checkpoint))
        if not targets:
            raise HTTPException(409, "没有可安全恢复的模型失败节点；工具失败或未知写入结果需先核查")
        try:
            await consume(db, root_id, "retries", app.state.context.limits.retries)
        except RuntimeLimitExceeded as exc:
            raise HTTPException(409, {"code": "RUNTIME_LIMIT_EXCEEDED", "message": str(exc)}) from exc
        reset = set()
        repaired_specs = {json.loads(row["plan_json"])["id"] for row, _, _ in targets if row.get("parent_task_id")}
        # A dependent that never started may wait again; cancelled/completed nodes are immutable here.
        while True:
            added = False
            for child in children:
                if child["id"] in reset or child["status"] != "failed" or child.get("answer") != "前置任务未成功，未执行该任务。":
                    continue
                spec = json.loads(child["plan_json"])
                if repaired_specs.intersection(spec.get("depends_on", [])):
                    reset.add(child["id"])
                    repaired_specs.add(spec["id"])
                    added = True
            if not added:
                break
        if children:
            await db.execute("UPDATE tasks SET status='running',interrupt_json=NULL WHERE id=?", (root_id,))
        for identifier in reset:
            await db.execute("UPDATE tasks SET status='blocked',answer=NULL,interrupt_json=NULL WHERE id=?", (identifier,))
        for row, kind, checkpoint in targets:
            await db.execute("UPDATE tasks SET status='queued',answer=NULL,interrupt_json=NULL WHERE id=?", (row["id"],))
            await db.publish(row["id"], "task.retry_requested", {"error_type": kind, "strategy": "checkpoint" if checkpoint else "replan"})
        for row, _, checkpoint in targets:
            if checkpoint:
                coroutine = coordinator.run_task(app, row["id"], row["session_id"], owner, row["query"], row["mode"],
                                                   continue_from_checkpoint=True)
            else:
                coroutine = coordinator.start(row["id"], row["session_id"], owner, row["query"], row["mode"])
            coordinator.spawn(row["id"], coroutine)
        if children:
            await coordinator.refresh(root_id)
        return {"task_id": task_id, "root_task_id": root_id, "status": "queued",
                "retried_task_ids": [row["id"] for row, _, _ in targets], "dependent_reset_ids": sorted(reset)}
