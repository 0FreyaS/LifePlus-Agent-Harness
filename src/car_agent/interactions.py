"""Guard all task mutations with one lifecycle lock and expiring user decisions."""
from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta, timezone

from fastapi import HTTPException

from .freshness import expired, lifecycle_lock, parse_time, utcnow
from .orchestration import TERMINAL, session_messages
from .user_events import selection_index, split_resume_message
from .runtime_limits import consume, RuntimeLimitExceeded


async def control_budget(app, task_id: str, counter: str):
    try:
        await consume(app.state.db, task_id, counter, getattr(app.state.context.limits, counter))
    except RuntimeLimitExceeded as exc:
        await app.state.db.publish(task_id, "runtime.limit_exceeded", {"limit": counter})
        raise HTTPException(409, {"code": "RUNTIME_LIMIT_EXCEEDED", "message": str(exc)}) from exc


async def owned_task(app, task_id: str, owner: str) -> dict:
    task = await app.state.db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


def pending_interrupt(task: dict, identifier: str) -> dict:
    pending = json.loads(task.get("interrupt_json") or "{}")
    if task["status"] != "waiting_user" or pending.get("id") != identifier:
        raise HTTPException(409, "任务或中断已变化，请刷新任务状态")
    return pending


def check_relative_date(task: dict) -> None:
    created = parse_time(task["created_at"])
    tz = timezone(timedelta(hours=8))
    if created and created.astimezone(tz).date() != utcnow().astimezone(tz).date() and re.search(r"今天|明天|后天|今日|今晚|\btomorrow\b|\btoday\b", task["query"], re.I):
        raise HTTPException(409, {"code": "RELATIVE_DATE_EXPIRED", "message": "请求已经跨日，相对日期不能自动沿用，请重新发送包含明确日期的完整请求。"})


async def resume_waiting(app, task_id: str, payload, owner: str, run_task) -> tuple[dict, str]:
    initial = await owned_task(app, task_id, owner)
    async with lifecycle_lock(app, initial):
        task = await owned_task(app, task_id, owner)
        pending = pending_interrupt(task, payload.interrupt_id)
        allowed = {"approval": {"approve", "reject"}, "selection": {"answer"}, "clarification": {"answer"}}
        if payload.action not in allowed.get(pending.get("kind"), set()):
            raise HTTPException(422, "操作类型与当前中断不匹配")
        if payload.action != "reject":
            check_relative_date(task)
        # Rejecting an expired approval is safe; approving or selecting is not.
        if payload.action != "reject" and expired(pending):
            await app.state.db.publish(task_id, "context.expired", {"kind": pending.get("kind"), "expires_at": pending.get("expires_at")})
            raise HTTPException(409, {"code": "INTERRUPT_EXPIRED", "message": "候选或确认已过期，请刷新当前任务后重新选择或确认。", "task_id": task_id})
        value, new_query = {**payload.value, "action": payload.action}, ""
        if payload.action == "answer" and value.get("text"):
            value["text"], new_query = split_resume_message(str(value["text"]).strip())
        if pending.get("kind") == "selection":
            chosen = selection_index(value, pending.get("candidates", []))
            if chosen is not None:
                value["index"] = chosen
            else:
                value.pop("index", None)
        if payload.action == "approve":
            value["operation_key"] = pending.get("operation_key")
            value["approval_expires_at"] = pending["expires_at"]
        if payload.action != "reject":
            await control_budget(app, task_id, "resumes")
        await app.state.db.execute("UPDATE tasks SET status='queued',interrupt_json=NULL WHERE id=?", (task_id,))
        app.state.coordinator.spawn(task_id, run_task(app, task_id, task["session_id"], owner, task["query"], task["mode"], value))
        await app.state.db.publish(task_id, "user.resumed", {"action": payload.action, "has_new_task": bool(new_query)})
        return {"task_id": task_id, "status": "queued"}, new_query


def effective_values(snapshot) -> dict:
    values = dict(snapshot.values)
    for task in snapshot.tasks:
        child = task.state
        if child is not None and hasattr(child, "values"):
            values.update(effective_values(child))
    return values


async def refresh_waiting(app, task_id: str, identifier: str, owner: str, run_task) -> dict:
    initial = await owned_task(app, task_id, owner)
    async with lifecycle_lock(app, initial):
        task = await owned_task(app, task_id, owner)
        pending_interrupt(task, identifier)
        check_relative_date(task)
        config = {"configurable": {"thread_id": task["thread_id"]}}
        snapshot = await app.state.graph.aget_state(config, subgraphs=True)
        state = effective_values(snapshot)
        if not state.get("domain"):
            raise HTTPException(409, "缺少可安全刷新的任务状态，请重新发送完整请求")
        slots = dict(state.get("slots", {}))
        slots.pop("selection", None)
        if state.get("action") == "local_search":
            slots.pop("poi_id", None)
        updates = {"slots": slots, "domain": state["domain"], "action": state.get("action", ""),
                   "read_generation": str(uuid.uuid4()), "tool_args": {}, "tool_result": {},
                   "operation_result": {}, "operation_key": "", "approval_expires_at": "",
                   "answer": "", "error": "", "status": "running"}
        context = app.state.context
        history = await session_messages(app.state.db, task["session_id"], completed_only=True,
                                         max_age_seconds=context.freshness.context_seconds)
        memory = await context.memory.build_context(owner, task["session_id"],
            [*history, {"role": "user", "content": task["query"]}], {"slots": slots})
        updates.update(memory_brief=memory["memory_brief"], model_history=memory["model_history"],
                       memory=memory["preferences"])
        await control_budget(app, task_id, "refreshes")
        # Re-enter the domain workflow, not the old interrupt with an old selection.
        await app.state.graph.aupdate_state(config, updates, as_node="route")
        await app.state.db.execute("UPDATE tasks SET status='queued',interrupt_json=NULL,answer=NULL WHERE id=?", (task_id,))
        await app.state.db.publish(task_id, "task.refresh_requested", {"previous_interrupt_id": identifier, "strategy": "reenter_domain_and_requery"})
        app.state.coordinator.spawn(task_id, run_task(app, task_id, task["session_id"], owner, task["query"], task["mode"], continue_from_checkpoint=True))
        return {"task_id": task_id, "status": "queued"}


async def cancel_running(app, task_id: str, owner: str) -> dict:
    import asyncio

    initial = await owned_task(app, task_id, owner)
    async with lifecycle_lock(app, initial):
        task = await owned_task(app, task_id, owner)
        if task["status"] in TERMINAL:
            raise HTTPException(409, "任务已经结束")
        runners = []
        root_id = task.get("parent_task_id") or task_id
        async with app.state.coordinator.locks.setdefault(root_id, asyncio.Lock()):
            await app.state.db.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=?", (utcnow().isoformat(), task_id))
            children = await app.state.db.fetchall("SELECT id FROM tasks WHERE parent_task_id=? AND status NOT IN ('completed','failed','cancelled')", (task_id,))
            for identifier in [task_id, *[child["id"] for child in children]]:
                await app.state.db.execute("UPDATE tasks SET status='cancelled',interrupt_json=NULL WHERE id=? AND status NOT IN ('completed','failed')", (identifier,))
                await app.state.context.tool_queue.cancel_task(identifier)
                runner = app.state.runners.get(identifier)
                if runner and not runner.done():
                    runner.cancel()
                    runners.append(runner)
        # Child finalizers refresh their parent; do not hold its scheduler lock here.
        await asyncio.gather(*runners, return_exceptions=True)
        await app.state.db.publish(task_id, "task.cancelled", {})
        if task.get("parent_task_id"):
            await app.state.coordinator.refresh(task["parent_task_id"])
        return {"task_id": task_id, "status": "cancelled"}
