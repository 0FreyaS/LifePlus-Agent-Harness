"""Persistent per-task work limits; human waiting does not consume active time."""
from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


class RuntimeLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeLimits:
    active_seconds: float = 120
    planning_seconds: float = 45
    model_calls: int = 8
    tool_calls: int = 24
    resumes: int = 8
    refreshes: int = 5
    retries: int = 3
    graph_steps: int = 64

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, "runtime_" + name) for name in cls.__dataclass_fields__})


COUNTERS = {"model_calls", "tool_calls", "resumes", "refreshes", "retries"}


async def usage(db, task_id: str) -> dict:
    await db.execute("INSERT OR IGNORE INTO runtime_usage(task_id) VALUES(?)", (task_id,))
    return await db.fetchone("SELECT * FROM runtime_usage WHERE task_id=?", (task_id,))


async def consume(db, task_id: str, counter: str, limit: int) -> None:
    if counter not in COUNTERS:
        raise ValueError("Unknown runtime counter")
    await usage(db, task_id)
    if not await db.execute(f"UPDATE runtime_usage SET {counter}={counter}+1 WHERE task_id=? AND {counter}<?", (task_id, limit)):
        raise RuntimeLimitExceeded(f"任务已达到 {counter} 上限（{limit}），请终止或创建明确的新任务")


@dataclass
class ExecutionScope:
    db: Any
    task_id: str
    limits: RuntimeLimits
    deadline: float
    domain: str = ""
    action: str = ""
    nodes: dict[str, float] = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def deadline_at(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=self.remaining())).isoformat()


_scope: ContextVar[ExecutionScope | None] = ContextVar("lifeplus_execution_scope", default=None)


def current_scope() -> ExecutionScope | None:
    return _scope.get()


async def record(kind: str, fallback=None, **data) -> None:
    scope = current_scope()
    if scope:
        await scope.db.publish(scope.task_id, kind, data)
    elif fallback:
        value = fallback(kind, **data)
        if inspect.isawaitable(value):
            await value


@asynccontextmanager
async def execution_scope(db, task_id: str, limits: RuntimeLimits, phase: str):
    previous = await usage(db, task_id)
    remaining = limits.active_seconds - previous["active_seconds"]
    if phase == "planning":
        remaining = min(remaining, limits.planning_seconds)
    if remaining <= 0:
        raise RuntimeLimitExceeded("任务累计执行时间预算已耗尽，等待用户的时间不计入预算")
    scope = ExecutionScope(db, task_id, limits, time.monotonic() + remaining)
    token = _scope.set(scope)
    started = time.monotonic()
    outcome = "completed"
    timer = asyncio.timeout(remaining)
    try:
        await record("runtime.started", phase=phase, remaining_seconds=round(remaining, 3))
        async with timer:
            yield scope
    except TimeoutError as exc:
        if not timer.expired():
            outcome = "failed"
            raise
        outcome = "budget_exhausted"
        await record("runtime.limit_exceeded", phase=phase, limit="active_seconds")
        raise RuntimeLimitExceeded("任务执行时间预算耗尽，已停止继续推理与提交工具") from exc
    except BaseException:
        outcome = "interrupted_or_failed"
        raise
    finally:
        elapsed = time.monotonic() - started
        try:
            await db.execute("UPDATE runtime_usage SET active_seconds=active_seconds+? WHERE task_id=?", (elapsed, task_id))
            await record("runtime.finished", phase=phase, outcome=outcome, elapsed_ms=round(elapsed * 1000))
        finally:
            _scope.reset(token)
