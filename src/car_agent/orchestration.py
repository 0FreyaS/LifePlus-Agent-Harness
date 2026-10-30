from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .task_planning import plan_tasks
from .freshness import recent, expired, stale_receipt
from .intent_schema import SlotValues
from .runtime_limits import execution_scope

TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


async def session_messages(db, session_id: str, completed_only: bool = False,
                           max_age_seconds: float | None = None) -> list[dict]:
    rows = await db.fetchall(
        "SELECT id,query,answer,status,created_at,updated_at FROM tasks WHERE session_id=? AND parent_task_id IS NULL "
        "ORDER BY created_at,id", (session_id,))
    result = []
    for row in rows:
        if completed_only and row["status"] not in TERMINAL:
            continue
        if max_age_seconds is not None and not recent(row["updated_at"], max_age_seconds):
            continue
        result.append({"id": row["id"] + ":user", "role": "user", "content": row["query"], "_time": row["created_at"]})
        if row.get("answer") and row["status"] in TERMINAL:
            result.append({"id": row["id"] + ":assistant", "role": "assistant", "content": row["answer"], "_time": row["updated_at"]})
    turns = await db.fetchall("SELECT request_id,message,created_at FROM conversation_inputs WHERE session_id=? ORDER BY created_at", (session_id,))
    result.extend({"id": "input:" + turn["request_id"] + ":user", "role": "user", "content": turn["message"], "_time": turn["created_at"]}
                  for turn in turns if max_age_seconds is None or recent(turn["created_at"], max_age_seconds))
    result.sort(key=lambda item: item["_time"])
    for item in result:
        item.pop("_time", None)
    return result


def task_view(row: dict) -> dict:
    item = dict(row)
    if item.get("interrupt_json"):
        item["interrupt"] = json.loads(item["interrupt_json"])
        item["interrupt"]["expired"] = expired(item["interrupt"])
    return item


class TaskCoordinator:
    def __init__(self, app: Any, run_task: Callable[..., Awaitable]):
        self.app = app
        self.db = app.state.db
        self.run_task = run_task
        self.locks: dict[str, asyncio.Lock] = {}

    def spawn(self, task_id: str, coroutine: Awaitable) -> None:
        task = asyncio.create_task(coroutine)
        self.app.state.runners[task_id] = task

        def finished(future):
            if self.app.state.runners.get(task_id) is future:
                self.app.state.runners.pop(task_id, None)

        task.add_done_callback(finished)

    async def start(self, task_id: str, session_id: str, owner: str, query: str, mode: str) -> None:
        try:
            await self.db.publish(task_id, "plan.started", {})
            row = await self.db.fetchone("SELECT plan_json FROM tasks WHERE id=?", (task_id,))
            prepared = json.loads(row["plan_json"] or "{}") if row else {}
            if prepared.get("input_request"):
                await self.db.publish(task_id, "plan.completed", {"source": "validated_form", "tasks": [
                    {key: prepared[key] for key in ("domain", "action", "slots")}]})
                await self.run_task(self.app, task_id, session_id, owner, query, mode)
                return
            history = await session_messages(self.db, session_id, completed_only=True,
                                             max_age_seconds=self.app.state.context.freshness.context_seconds)
            memory = await self.app.state.context.memory.build_context(owner, session_id, history)
            brief = memory["memory_brief"] + "\n用户偏好：" + json.dumps(memory["preferences"], ensure_ascii=False)
            brief += "\n有效会话引用：" + json.dumps(memory["conversation_references"], ensure_ascii=False)
            async with execution_scope(self.db, task_id, self.app.state.context.limits, "planning"):
                context = self.app.state.context
                gate_options = {"gate": context.planning_gate} if getattr(context, "planning_gate", None) else {}
                if context.planner_variant == "compact":
                    plan = await plan_tasks(query, mode, context.model_for("planner"), brief, variant="compact", **gate_options)
                else:
                    plan = await plan_tasks(query, mode, context.model_for("planner"), brief, **gate_options)
            if plan is None or len(plan.tasks) == 1:
                if plan:
                    await self.db.execute("UPDATE tasks SET plan_json=? WHERE id=?",
                                          (json.dumps(plan.tasks[0].model_dump(), ensure_ascii=False), task_id))
                await self.run_task(self.app, task_id, session_id, owner, query, mode)
                return
            async with self.locks.setdefault(task_id, asyncio.Lock()):
                current = await self.db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
                if not current or current["status"] in TERMINAL:
                    return
                now = datetime.now(timezone.utc).isoformat()
                await self.db.execute("UPDATE tasks SET status='running',domain='multi',plan_json=? WHERE id=?",
                                      (plan.model_dump_json(), task_id))
                for spec in plan.tasks:
                    child_id = str(uuid.uuid4())
                    await self.db.execute(
                        "INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at,"
                        "request_id,parent_task_id,thread_id,plan_json,domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (child_id, session_id, owner, spec.query, "blocked", mode, now, now,
                         str(uuid.uuid4()), task_id, session_id + ":" + child_id, spec.model_dump_json(), spec.domain))
                await self.db.publish(task_id, "plan.completed", {"tasks": plan.model_dump()["tasks"]})
            await self.refresh(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            answer = "任务规划没有完成：" + str(exc)[:300]
            if not await self.db.execute("UPDATE tasks SET status='failed',answer=? WHERE id=? AND status!='cancelled'", (answer, task_id)):
                return
            await self.db.publish(task_id, "task.failed", {"answer": answer, "error": type(exc).__name__})

    async def _bind(self, spec: dict, children: dict[str, dict]) -> dict:
        decision = {"domain": spec["domain"], "bound_slots": {}}
        # Forms and persisted pre-migration plans remain trusted prepared decisions.
        if spec.get("action"):
            decision.update(action=spec["action"], slots=dict(spec.get("slots", {})))
        for slot, path in spec.get("bind_slots", {}).items():
            source, *keys = path.split(".")
            child = children[source]
            snapshot = await self.app.state.graph.aget_state({"configurable": {"thread_id": child["thread_id"]}})
            state = snapshot.values
            receipt = state.get("tool_result", {})
            if not isinstance(receipt, dict):
                raise ValueError("前置工具回执格式不合法，未执行依赖任务")
            route_receipt = receipt.get("route_result") or {}
            if not isinstance(route_receipt, dict):
                raise ValueError("前置路线回执格式不合法，未执行依赖任务")
            if stale_receipt(receipt) or stale_receipt(route_receipt):
                raise ValueError("前置证据已过期，未执行依赖任务；请重新查询或规划后再确认。")
            items = receipt.get("items") or []
            index = int(state.get("slots", {}).get("selection", 1)) - 1
            value = {"selected": items[index] if 0 <= index < len(items) else {},
                     "route": state.get("tool_result", {}).get("route_result", {}),
                     "result": state.get("tool_result", {}), "slots": state.get("slots", {})}
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    raise ValueError(f"前置任务未提供绑定字段：{path}")
                value = value[key]
            if value is None or value == "" or value == {}:
                raise ValueError(f"前置任务未提供有效绑定证据：{path}")
            try:
                value = SlotValues.model_validate({slot: value}, strict=True).model_dump(exclude_none=True)[slot]
            except (ValueError, KeyError) as exc:
                raise ValueError(f"绑定接收字段{slot}与来源{path}的类型不兼容") from exc
            decision["bound_slots"][slot] = value
            if "slots" in decision:
                decision["slots"][slot] = value
        return decision

    async def refresh(self, parent_id: str) -> None:
        if self.app.state.closing:
            return
        async with self.locks.setdefault(parent_id, asyncio.Lock()):
            parent = await self.db.fetchone("SELECT * FROM tasks WHERE id=?", (parent_id,))
            if not parent or parent["status"] in TERMINAL:
                return
            rows = await self.db.fetchall("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at,rowid", (parent_id,))
            children = {json.loads(row["plan_json"])["id"]: row for row in rows}
            for row in rows:
                if row["status"] != "blocked":
                    continue
                spec = json.loads(row["plan_json"])
                dependencies = [children[key] for key in spec.get("depends_on", [])]
                if any(dependency["status"] in TERMINAL - {"completed"} for dependency in dependencies):
                    row["status"], row["answer"] = "failed", "前置任务未成功，未执行该任务。"
                    await self.db.execute("UPDATE tasks SET status='failed',answer=? WHERE id=?", (row["answer"], row["id"]))
                    await self.db.publish(row["id"], "task.failed", {"answer": row["answer"]})
                    continue
                if not all(dependency["status"] == "completed" for dependency in dependencies):
                    continue
                try:
                    decision = await self._bind(spec, children)
                except ValueError as exc:
                    row["status"], row["answer"] = "failed", str(exc)
                    await self.db.execute("UPDATE tasks SET status='failed',answer=? WHERE id=?", (row["answer"], row["id"]))
                    continue
                spec["prepared_decision"] = decision
                row["status"] = "queued"
                await self.db.execute("UPDATE tasks SET status='queued',plan_json=? WHERE id=?",
                                      (json.dumps(spec, ensure_ascii=False), row["id"]))
                self.spawn(row["id"], self.run_task(self.app, row["id"], row["session_id"],
                                                   row["user_id"], row["query"], row["mode"]))
            statuses = [row["status"] for row in rows]
            if any(status in {"queued", "running"} for status in statuses):
                status = "running"
            elif any(status in {"waiting_user", "blocked"} for status in statuses):
                status = "waiting_user"
            elif all(status == "completed" for status in statuses):
                status = "completed"
            elif "completed" in statuses:
                status = "partial"
            elif all(status == "cancelled" for status in statuses):
                status = "cancelled"
            else:
                status = "failed"
            labels = {"waiting_user": "等待补充信息", "blocked": "等待前置任务", "queued": "已排队", "running": "执行中", "cancelled": "已取消"}
            answer = "\n\n".join(f"{index}. {row['query']}\n{row.get('answer') or labels.get(row['status'], row['status'])}"
                                  for index, row in enumerate(rows, 1))
            await self.db.execute("UPDATE tasks SET status=?,answer=?,updated_at=? WHERE id=?",
                                  (status, answer, datetime.now(timezone.utc).isoformat(), parent_id))
            await self.db.publish(parent_id, "group.updated", {
                "status": status, "children": [task_view(row) for row in rows],
                "completed": statuses.count("completed"), "total": len(rows),
            })
            if status in TERMINAL:
                await self.db.publish(parent_id, "answer.completed", {"answer": answer, "status": status})
                await self.db.publish(parent_id, "task." + status, {"answer": answer})
                history = await session_messages(self.db, parent["session_id"], completed_only=True,
                                                 max_age_seconds=self.app.state.context.freshness.context_seconds)
                await self.app.state.context.memory.save_context(parent["user_id"], parent["session_id"], history, {})
