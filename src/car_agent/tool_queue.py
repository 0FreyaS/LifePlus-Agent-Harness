from __future__ import annotations

import asyncio
import hashlib
import json
import random
from time import monotonic
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .persistence import Database
from .freshness import FreshnessPolicy, stale_receipt, expired, parse_time
from .runtime_limits import consume, current_scope
from .tool_policy import require_tool, retryable_error, normalize_tool_result

ToolDispatcher = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
EventPublisher = Callable[[str, str, dict[str, Any]], Awaitable[dict]]


class ToolFailure(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__(str(result["error"]))
        self.result = result


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolQueue:
    """Durable async tool queue with dependencies, retries, budgets and DLQ."""

    def __init__(self, db: Database, dispatcher: ToolDispatcher, publisher: EventPublisher,
                 workers: int = 4, default_timeout: float = 8.0, default_retries: int = 2,
                 freshness: FreshnessPolicy | None = None):
        self.db = db
        self.dispatcher = dispatcher
        self.publisher = publisher
        self.worker_count = max(1, workers)
        self.default_timeout = default_timeout
        self.default_retries = default_retries
        self.freshness = freshness or FreshnessPolicy()
        self._workers: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._closed = False
        self._limits: defaultdict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(4))
        self._active: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        from .tools import TOOL_CATALOG

        for row in await self.db.fetchall("SELECT * FROM tool_jobs WHERE status='running'"):
            if TOOL_CATALOG.get(row["tool_name"], (None, None, False))[2]:
                await self._dead_letter(row, "进程中断前写入结果未知，需要核对账本，不能自动重放")
        await self.db.execute(
            "UPDATE tool_jobs SET status='queued',available_at=?,updated_at=? WHERE status='running'",
            (utcnow(), utcnow()),
        )
        self._workers = [asyncio.create_task(self._worker(index), name=f"lifeplus-tool-{index}")
                         for index in range(self.worker_count)]

    async def close(self) -> None:
        self._closed = True
        self._wake.set()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def submit(self, *, task_id: str, tool_name: str, arguments: dict[str, Any],
                     idempotency_key: str, dependencies: list[str] | None = None,
                     timeout_seconds: float | None = None, max_retries: int | None = None,
                     expires_at: str | None = None, deadline_at: str | None = None) -> str:
        from .tools import TOOL_CATALOG

        policy = TOOL_CATALOG.get(tool_name)
        scope = current_scope()
        if scope:
            if task_id != scope.task_id:
                raise PermissionError("工具作业必须属于当前执行任务")
            require_tool(scope.domain, scope.action, tool_name)
            if not policy:
                raise PermissionError("工具未注册")
            # Validate before making a durable job; the MCP server validates again.
            policy[0].model_validate(arguments)
            await consume(self.db, task_id, "tool_calls", scope.limits.tool_calls)
            await self._emit(task_id, "tool.policy.checked", {"tool_name": tool_name, "domain": scope.domain,
                "action": scope.action, "write": policy[2],
                "schema_hash": hashlib.sha256(json.dumps(policy[0].model_json_schema(), sort_keys=True).encode()).hexdigest()[:16]})
            deadline_at = min(filter(None, [deadline_at, scope.deadline_at()]), key=lambda value: parse_time(value))
        if dependencies:
            if len(dependencies) > 24 or len(set(dependencies)) != len(dependencies):
                raise ValueError("工具依赖过多或重复")
            marks = ",".join("?" for _ in dependencies)
            parents = await self.db.fetchall(f"SELECT id,task_id FROM tool_jobs WHERE id IN ({marks})", tuple(dependencies))
            if len(parents) != len(dependencies) or any(row["task_id"] != task_id for row in parents):
                raise ValueError("工具依赖必须是同一任务内已经创建的作业")
        selected_timeout = timeout_seconds if timeout_seconds is not None else (
            policy[3] if policy else self.default_timeout)
        retries = max_retries if max_retries is not None else self.default_retries
        if selected_timeout <= 0 or retries < 0 or retries > 10:
            raise ValueError("工具超时和重试配置不合法")
        if deadline_at is None:
            budget = selected_timeout * (retries + 1) + sum(min(2 ** i * 1.5, 8) for i in range(retries)) + 2
            deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=budget)).isoformat()
        if not parse_time(deadline_at):
            raise ValueError("工具执行期限必须是带时区的时间")
        base_key = idempotency_key
        for _ in range(128):
            job_id = str(uuid.uuid4())
            now = utcnow()
            await self.db.execute(
                "INSERT OR IGNORE INTO tool_jobs(id,task_id,tool_name,args_json,dependencies_json,idempotency_key,status,"
                "attempt,max_retries,timeout_seconds,available_at,created_at,updated_at,expires_at,deadline_at) "
                "VALUES(?,?,?,?,?,?,'queued',0,?,?,?,?,?,?,?)",
                (job_id, task_id, tool_name, json.dumps(arguments, ensure_ascii=False),
                 json.dumps(dependencies or []), idempotency_key,
                 max_retries if max_retries is not None else self.default_retries,
                 selected_timeout, now, now, now, expires_at, deadline_at),
            )
            existing = await self.db.fetchone("SELECT * FROM tool_jobs WHERE idempotency_key=?", (idempotency_key,))
            assert existing
            if (existing["task_id"] != task_id or existing["tool_name"] != tool_name
                    or json.loads(existing["args_json"]) != arguments):
                raise ValueError("工具幂等键与原任务或参数冲突")
            if existing["id"] == job_id:
                break
            if policy and not policy[2] and existing["status"] == "completed":
                result = json.loads(existing.get("result_json") or "{}")
                if "_freshness" not in result:
                    result = self.freshness.receipt(tool_name, result, existing["updated_at"])
                if self.freshness.tool_seconds(tool_name) is None or stale_receipt(result):
                    idempotency_key = "read-refresh:" + hashlib.sha256((base_key + existing["id"]).encode()).hexdigest()
                    continue
            return existing["id"]
        else:
            raise RuntimeError("读缓存代次过多，已停止继续查找，请创建新任务")
        await self._emit(task_id, "tool.queued", {"job_id": job_id, "tool_name": tool_name,
                                                   "dependencies": dependencies or []})
        self._wake.set()
        return job_id

    async def wait(self, job_id: str, total_budget_seconds: float | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        job = await self.db.fetchone("SELECT timeout_seconds,max_retries,deadline_at FROM tool_jobs WHERE id=?", (job_id,))
        retries = int(job["max_retries"]) if job else self.default_retries
        timeout = float(job["timeout_seconds"]) if job else self.default_timeout
        budget = timeout * (retries + 1) + sum(min(2 ** i, 8) for i in range(retries)) + 2
        deadline = loop.time() + (total_budget_seconds if total_budget_seconds is not None else budget)
        if job and job.get("deadline_at"):
            deadline = min(deadline, loop.time() + max(0, (parse_time(job["deadline_at"]) - datetime.now(timezone.utc)).total_seconds()))
        while loop.time() < deadline:
            row = await self.db.fetchone("SELECT status,result_json,error FROM tool_jobs WHERE id=?", (job_id,))
            if not row:
                return {"error": "工具任务不存在", "job_id": job_id}
            if row["status"] == "completed":
                return json.loads(row["result_json"] or "{}")
            if row["status"] in {"failed", "dead_letter", "cancelled"}:
                return {**json.loads(row["result_json"] or "{}"),
                        "error": row["error"] or "工具任务失败", "job_id": job_id,
                        "queue_status": row["status"]}
            await asyncio.sleep(0.05)
        await self.cancel_job(job_id, "工具任务超过执行预算，停止等待及后续执行")
        return {"error": "工具任务超过执行预算", "error_code": "BUDGET_EXCEEDED", "retryable": False,
                "job_id": job_id, "queue_status": "timeout"}

    async def execute(self, *, total_budget_seconds: float | None = None, **kwargs: Any) -> dict[str, Any]:
        if total_budget_seconds is not None:
            kwargs["deadline_at"] = (datetime.now(timezone.utc) + timedelta(seconds=total_budget_seconds)).isoformat()
        job_id = await self.submit(**kwargs)
        try:
            return await self.wait(job_id, total_budget_seconds)
        except asyncio.CancelledError:
            await self.cancel_job(job_id, "调用方已取消，停止后续执行")
            raise

    async def cancel_job(self, job_id: str, reason: str) -> None:
        row = await self.db.fetchone("SELECT * FROM tool_jobs WHERE id=?", (job_id,))
        if not row:
            return
        changed = await self.db.execute("UPDATE tool_jobs SET status='cancelled',error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                                        (reason, utcnow(), job_id))
        if changed:
            active = self._active.get(job_id)
            if active:
                active.cancel()
            await self._emit(row["task_id"], "tool.budget_exhausted", {"job_id": job_id,
                "tool_name": row["tool_name"], "outcome_unknown": row["status"] == "running"})

    async def execute_many(self, jobs: list[dict[str, Any]],
                           total_budget_seconds: float | None = None) -> list[dict[str, Any]]:
        """Run independent jobs concurrently while keeping each job durable and traceable."""
        job_ids = []
        try:
            # Submission is cheap; keep ownership of every created job if a later input is invalid.
            for job in jobs:
                job_ids.append(await self.submit(**job))
            return await asyncio.gather(*(self.wait(job_id, total_budget_seconds) for job_id in job_ids))
        except BaseException:
            await asyncio.gather(*(self.cancel_job(job_id, "调用方已取消") for job_id in job_ids))
            raise

    async def cancel_task(self, task_id: str) -> None:
        rows = await self.db.fetchall("SELECT id,status,tool_name FROM tool_jobs WHERE task_id=? AND status IN ('queued','running')", (task_id,))
        await self.db.execute("UPDATE tool_jobs SET status='cancelled',error='用户取消工具任务',updated_at=? WHERE task_id=? AND status IN ('queued','running')",
                              (utcnow(), task_id))
        for row in rows:
            active = self._active.get(row["id"])
            if active:
                active.cancel()
            await self._emit(task_id, "tool.cancelled", {"job_id": row["id"], "tool_name": row["tool_name"],
                                                         "outcome_unknown": row["status"] == "running"})

    async def _dependencies_ready(self, row: dict[str, Any]) -> tuple[bool, str | None]:
        dependencies = json.loads(row.get("dependencies_json") or "[]")
        if not dependencies:
            return True, None
        marks = ",".join("?" for _ in dependencies)
        jobs = await self.db.fetchall(f"SELECT id,status FROM tool_jobs WHERE id IN ({marks})", tuple(dependencies))
        statuses = {job["id"]: job["status"] for job in jobs}
        if any(statuses.get(job_id) in {"failed", "dead_letter", "cancelled"} for job_id in dependencies):
            return False, "依赖任务失败"
        return all(statuses.get(job_id) == "completed" for job_id in dependencies), None

    async def _worker(self, index: int) -> None:
        while not self._closed:
            row = await self.db.claim_tool_job(utcnow())
            if not row:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                continue
            if row.get("deadline_at") and expired({"expires_at": row["deadline_at"]}):
                await self._dead_letter(row, "BUDGET_EXCEEDED: 作业执行预算耗尽，未调用外部工具")
                continue
            if row.get("expires_at") and expired({"expires_at": row["expires_at"]}):
                await self._dead_letter(row, "用户确认已过期，请重新确认，未执行写入")
                continue
            ready, dependency_error = await self._dependencies_ready(row)
            if dependency_error:
                await self._dead_letter(row, dependency_error)
                continue
            if not ready:
                await self.db.execute("UPDATE tool_jobs SET status='queued',attempt=MAX(0,attempt-1),available_at=?,updated_at=? WHERE id=?",
                                      ((datetime.now(timezone.utc) + timedelta(milliseconds=100)).isoformat(), utcnow(), row["id"]))
                continue
            execution = asyncio.create_task(self._run(row))
            self._active[row["id"]] = execution
            try:
                await execution
            except asyncio.CancelledError:
                if self._closed:
                    raise
            finally:
                self._active.pop(row["id"], None)

    async def _run(self, row: dict[str, Any]) -> None:
        task_id, tool_name = row["task_id"], row["tool_name"]
        arguments = json.loads(row["args_json"])
        attempt = int(row["attempt"]) + 1
        waiting_started = monotonic()
        claimed_at = datetime.now(timezone.utc)
        current = await self.db.fetchone("SELECT status FROM tool_jobs WHERE id=?", (row["id"],))
        if not current or current["status"] == "cancelled":
            return
        try:
            async with self._limits[tool_name]:
                if row.get("expires_at") and expired({"expires_at": row["expires_at"]}):
                    await self._dead_letter(row, "用户确认已过期，请重新确认，未执行写入")
                    return
                remaining = ((parse_time(row["deadline_at"]) - datetime.now(timezone.utc)).total_seconds()
                             if row.get("deadline_at") else float(row["timeout_seconds"]))
                if remaining <= 0:
                    await self._dead_letter(row, "BUDGET_EXCEEDED: 执行预算耗尽，未调用外部工具")
                    return
                ready_at = parse_time(row["available_at"])
                await self._emit(task_id, "tool.started", {"job_id": row["id"], "tool_name": tool_name,
                    "arguments": arguments, "attempt": attempt,
                    "ready_queue_ms": max(0, round((claimed_at - ready_at).total_seconds() * 1000)),
                    "semaphore_wait_ms": round((monotonic() - waiting_started) * 1000)})
                dispatched = monotonic()
                result = await asyncio.wait_for(
                    self.dispatcher(tool_name, arguments, task_id),
                    timeout=min(float(row["timeout_seconds"]), remaining),
                )
            result = normalize_tool_result(result)
            if (result.get("error") or result.get("ok") is False) and not result.get("requires_clarification"):
                raise ToolFailure({"error": "工具报告失败", **result})
            result = self.freshness.receipt(tool_name, result)
            changed = await self.db.execute(
                "UPDATE tool_jobs SET status='completed',result_json=?,error=NULL,updated_at=? WHERE id=? AND status='running'",
                (json.dumps(result, ensure_ascii=False), utcnow(), row["id"]),
            )
            if not changed:
                await self.db.execute("UPDATE tool_jobs SET result_json=?,updated_at=? WHERE id=? AND status='cancelled'",
                                      (json.dumps(result, ensure_ascii=False), utcnow(), row["id"]))
                await self._emit(task_id, "tool.late_result", {"job_id": row["id"], "tool_name": tool_name,
                                                               "result": result, "status": "cancelled"})
                return
            await self._emit(task_id, "tool.clarification_required" if result.get("requires_clarification") else "tool.completed",
                {"job_id": row["id"], "tool_name": tool_name, "result": result, "attempt": attempt,
                 "dispatch_ms": round((monotonic() - dispatched) * 1000)})
        except asyncio.CancelledError:
            timing = {"dispatch_ms": round((monotonic() - dispatched) * 1000)} if "dispatched" in locals() else {}
            await self._emit(task_id, "tool.attempt.cancelled", {"job_id": row["id"], "tool_name": tool_name,
                "attempt": attempt, "outcome_unknown": "dispatched" in locals(), **timing})
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            current = await self.db.fetchone("SELECT status FROM tool_jobs WHERE id=?", (row["id"],))
            if not current or current["status"] == "cancelled":
                return
            code, retryable = retryable_error(exc, exc.result if isinstance(exc, ToolFailure) else None)
            await self._emit(task_id, "tool.attempt.failed", {"job_id": row["id"], "tool_name": tool_name,
                "attempt": attempt, "error_code": code, "error_type": type(exc).__name__,
                "dispatch_ms": round((monotonic() - dispatched) * 1000) if "dispatched" in locals() else 0})
            from .tools import TOOL_CATALOG

            if TOOL_CATALOG.get(tool_name, (None, None, False))[2]:
                retryable = False
                await self._emit(task_id, "tool.write_failed", {"job_id": row["id"],
                    "tool_name": tool_name, "outcome_unknown": not isinstance(exc, ToolFailure), "error": error})
            if isinstance(exc, ToolFailure):
                await self.db.execute("UPDATE tool_jobs SET result_json=? WHERE id=?",
                                      (json.dumps(exc.result, ensure_ascii=False), row["id"]))
            if retryable and attempt <= int(row["max_retries"]):
                delay = min(2 ** (attempt - 1) * random.uniform(0.5, 1.5), 8)
                if isinstance(exc, ToolFailure) and isinstance(exc.result.get("retry_after_seconds"), (int, float)):
                    delay = max(delay, max(0, exc.result["retry_after_seconds"]))
                deadline = parse_time(row.get("deadline_at"))
                if deadline and (deadline - datetime.now(timezone.utc)).total_seconds() <= delay:
                    await self._dead_letter(row, "BUDGET_EXCEEDED: 剩余预算不足以执行下一次重试", attempts=attempt)
                    return
                available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                changed = await self.db.execute(
                    "UPDATE tool_jobs SET status='queued',error=?,available_at=?,updated_at=? WHERE id=? AND status='running'",
                    (error, available_at, utcnow(), row["id"]),
                )
                if not changed:
                    return
                await self._emit(task_id, "tool.retry_scheduled", {"job_id": row["id"],
                    "tool_name": tool_name, "attempt": attempt, "delay_seconds": delay,
                    "error": error, "error_code": code,
                    "dispatch_ms": round((monotonic() - dispatched) * 1000) if "dispatched" in locals() else 0})
                self._wake.set()
            else:
                await self._dead_letter(row, error, attempts=attempt)

    async def _dead_letter(self, row: dict[str, Any], error: str, attempts: int | None = None) -> None:
        attempts = attempts if attempts is not None else int(row.get("attempt", 0)) + 1
        now = utcnow()
        changed = await self.db.execute("UPDATE tool_jobs SET status='dead_letter',error=?,updated_at=? WHERE id=? AND status='running'",
                                        (error, now, row["id"]))
        if not changed:
            return
        await self.db.execute(
            "INSERT OR REPLACE INTO tool_dead_letters(job_id,task_id,tool_name,args_json,error,attempts,failed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (row["id"], row["task_id"], row["tool_name"], row["args_json"], error, attempts, now),
        )
        await self._emit(row["task_id"], "tool.dead_letter", {"job_id": row["id"],
            "tool_name": row["tool_name"], "attempts": attempts, "error": error})

    async def _emit(self, task_id: str, event_type: str, data: dict[str, Any]) -> None:
        try:
            await self.publisher(task_id, event_type, data)
        except KeyError:
            # Direct graph tests may not create an API task row. Queue execution still remains valid.
            return
