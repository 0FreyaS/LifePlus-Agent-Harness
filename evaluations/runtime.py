"""Real graphs/checkpoints/queue with explicitly controlled external receipts."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from car_agent.harness import build_graph
from car_agent.memory import MemoryRepository
from car_agent.persistence import Database
from car_agent.runtime_context import RunContext
from car_agent.skill_registry import LifeSkillRegistry
from car_agent.tool_queue import ToolQueue


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class RuntimeLab:
    async def __aenter__(self):
        self.temp = TemporaryDirectory(prefix="lifeplus-eval-")
        self.path = str(Path(self.temp.name) / "runtime.sqlite3")
        self.db = Database(self.path)
        await self.db.open()
        self.saver_cm = AsyncSqliteSaver.from_conn_string(self.path)
        self.saver = await self.saver_cm.__aenter__()
        await self.saver.setup()
        self.memory = MemoryRepository(self.db)
        await self.memory.open()
        self.events = []
        self.calls = []
        self.count = 1
        self.handler = None
        self.lock = asyncio.Lock()

        async def publish(task_id, kind, data):
            self.events.append({"task_id": task_id, "type": kind, **data})

        async def dispatch(name, args, task_id):
            self.calls.append({"tool": name, "args": args, "task_id": task_id})
            if self.handler:
                return await self.handler(name, args, task_id)
            return self.receipt(name, args)

        self.queue = ToolQueue(self.db, dispatch, publish, workers=4, default_retries=0)
        await self.queue.start()
        root = Path(__file__).resolve().parents[1]
        self.context = RunContext("eval", self.db, self.memory, None, None, self.queue,
            LifeSkillRegistry(str(root / "skills")), SimpleNamespace(search=lambda _: []))
        self.graph = build_graph(self.saver)
        return self

    async def __aexit__(self, *_):
        await self.queue.close()
        await self.memory.close()
        await self.saver_cm.__aexit__(None, None, None)
        await self.db.close()
        self.temp.cleanup()

    def reset(self):
        self.handler = None
        self.count = 1
        self.calls.clear()
        self.events.clear()

    def receipt(self, name, args):
        items = [{"id": f"poi-{i}", "name": f"测试候选{i}", "singer": "测试歌手",
                  "address": f"测试地址{i}", "location": f"120.{300+i},31.500",
                  "city": "无锡", "url": "", "rating": None, "cost": None}
                 for i in range(1, self.count + 1)]
        if name == "maps_place_detail":
            return {"ok": True, "item": {"id": args["poi_id"], "name": "详情", "location": "120.3,31.5"}}
        if name in {"maps_search", "search_music", "maps_local_search"}:
            return {"ok": True, "items": items, "center": "120.3,31.5"}
        if name == "maps_route":
            return {"ok": True, "distance_m": 2000, "duration_s": 300, "travel_mode": "driving"}
        if name == "maps_weather":
            return {"ok": True, "city": args.get("city", "无锡"), "date": "2030-01-01", "day_weather": "晴", "low": 10, "high": 20}
        return {"ok": True, "status": {"save_itinerary": "saved", "create_reminder": "scheduled", "cancel_reminder": "cancelled"}.get(name), **args}

    async def invoke(self, case_id, decision, query="受控流程评测", graph=None):
        state = {"messages": [HumanMessage(content=query)], "user_id": "eval", "session_id": case_id,
                 "task_id": case_id, "request_id": case_id, "mode": "demo", "original_query": query,
                 "status": "running", "slots": {}, "prepared_decision": decision}
        config = {"configurable": {"thread_id": case_id}}
        graph = graph or self.graph
        await graph.ainvoke(state, config=config, context=self.context)
        return await graph.aget_state(config)

    async def resume(self, case_id, value, graph=None):
        graph = graph or self.graph
        config = {"configurable": {"thread_id": case_id}}
        await graph.ainvoke(Command(resume=value), config=config, context=self.context)
        return await graph.aget_state(config)

    @staticmethod
    def interruption(snapshot):
        return next((interrupt.value for task in snapshot.tasks for interrupt in task.interrupts), None)

    async def clarification(self, case, decision):
        async with self.lock:
            self.reset()
            snapshot = await self.invoke(case["id"], decision, case["query"])
            waiting = self.interruption(snapshot)
            missing = waiting.get("field") if waiting and waiting.get("kind") == "clarification" else None
            return {"missing_field": missing, "interrupt": waiting,
                    "tool_calls": list(self.calls), "status": snapshot.values.get("status")}

    async def selection(self, case):
        self.reset()
        scenario, domain = case["expected"]["scenario"], case["domain"]
        self.count = 0 if scenario == "empty" else 1 if scenario == "single" else 3
        action = {"navigation": "navigation_search", "music": "music_search", "local_life": "local_search"}[domain]
        slots = {"destination": "东南大学", "keyword": "周杰伦", "category": "restaurant", "anchor": "无锡站"}
        snapshot = await self.invoke(case["id"], {"domain": domain, "action": action, "slots": slots})
        if scenario in {"single", "empty"}:
            require(not self.interruption(snapshot), "零/单个候选不应要求选择")
            require(snapshot.values["status"] == "completed", "查询未完成")
        else:
            require(self.interruption(snapshot)["kind"] == "selection", "未等待候选选择")
            choices = {"first": ({"index": 1}, 1), "second": ({"index": 2}, 2),
                       "last": ({"index": 3}, 3), "ordinal": ({"text": "第二个"}, 2),
                       "numeric_text": ({"text": "3"}, 3)}
            invalid = {"invalid_zero": {"index": 0}, "invalid_large": {"index": 999}, "invalid_text": {"text": "不知道"}}
            if scenario in invalid:
                snapshot = await self.resume(case["id"], invalid[scenario])
                require(self.interruption(snapshot) is not None, "非法选择绕过了中断")
                require(len(self.calls) == 1, "非法选择不应重新请求搜索")
            else:
                value, expected_index = choices[scenario]
                snapshot = await self.resume(case["id"], value)
                require(snapshot.values["slots"]["selection"] == expected_index, "候选索引绑定错误")
                require(snapshot.values["status"] == "completed", "选择后未完成")
                if domain == "local_life":
                    require(self.calls[-1]["args"]["poi_id"] == f"poi-{expected_index}", "商家详情ID绑定错误")
        return {"state": snapshot.values["status"], "slots": snapshot.values["slots"], "calls": list(self.calls)}

    async def recovery(self, case):
        self.reset()
        scenario, action, cid = case["expected"]["scenario"], case["action"], case["id"]
        slots = {"title": f"评测{cid}", "remind_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                 "reminder_id": "eval-reminder"}
        decision = {"domain": "planning", "action": action, "slots": slots}
        if scenario in {"approve", "reject", "unapproved", "bad_key", "reopen_checkpoint", "isolated_threads"}:
            snapshot = await self.invoke(cid, decision)
            waiting = self.interruption(snapshot)
            require(waiting and waiting["kind"] == "approval", "写操作未等待确认")
            require(not self.calls, "未经确认已执行写操作")
            if scenario == "unapproved":
                return {"waiting": waiting["kind"], "writes": 0}
            if scenario == "isolated_threads":
                other = await self.invoke(cid + "-other", decision)
                other_wait = self.interruption(other)
                await self.resume(cid + "-other", {"action": "approve", "operation_key": other_wait["operation_key"]})
                original = await self.graph.aget_state({"configurable": {"thread_id": cid}})
                require(self.interruption(original) == waiting, "其他任务污染了原任务中断")
                require(len(self.calls) == 1, "独立任务写入数量错误")
                return {"writes": 1, "original_still_waiting": True}
            value = {"action": "reject" if scenario == "reject" else "approve",
                     "operation_key": "wrong" if scenario == "bad_key" else waiting["operation_key"]}
            if scenario == "reopen_checkpoint":
                async with AsyncSqliteSaver.from_conn_string(self.path) as saver:
                    graph = build_graph(saver)
                    snapshot = await self.resume(cid, value, graph)
            else:
                snapshot = await self.resume(cid, value)
            if scenario == "bad_key":
                renewed = self.interruption(snapshot)
                require(renewed and renewed["kind"] == "approval", "错误凭证后没有重新进入可恢复确认节点")
                require(not self.calls, "错误凭证执行了写操作")
                snapshot = await self.resume(cid, {"action": "approve", "operation_key": renewed["operation_key"]})
                require(snapshot.values["status"] == "completed" and len(self.calls) == 1, "重新确认后未恰好执行一次")
                return {"reconfirmation": True, "writes": 1}
            expected_writes = 0 if scenario in {"reject", "bad_key"} else 1
            require(len(self.calls) == expected_writes, "写入次数不符合确认状态")
            require(snapshot.values["status"] == {"reject": "cancelled", "bad_key": "waiting_user"}.get(scenario, "completed"), "恢复状态错误")
            return {"status": snapshot.values["status"], "writes": len(self.calls)}
        args = {"user_id": "eval", "title": cid}
        job = {"task_id": cid, "tool_name": action, "arguments": args, "idempotency_key": cid}
        if scenario in {"queue_dedup", "queue_conflict"}:
            ids = await asyncio.gather(*(self.queue.submit(**job) for _ in range(4)))
            require(len(set(ids)) == 1, "并发提交没有去重")
            await self.queue.wait(ids[0])
            require(len(self.calls) == 1, "幂等重放重复执行")
            if scenario == "queue_conflict":
                try:
                    await self.queue.submit(**{**job, "arguments": {"title": "changed"}})
                except ValueError:
                    pass
                else:
                    raise AssertionError("冲突幂等键未拒绝")
            return {"submissions": 4, "executions": len(self.calls)}
        started, finished = asyncio.Event(), asyncio.Event()

        async def delayed(name, arguments, task_id):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if scenario == "late_success":
                    finished.set()
                    return self.receipt(name, arguments)
                finished.set()
                raise

        self.handler = delayed
        job_id = await self.queue.submit(**job)
        await asyncio.wait_for(started.wait(), 2)
        await self.queue.cancel_task(cid)
        await asyncio.wait_for(finished.wait(), 2)
        for _ in range(100):
            if job_id not in self.queue._active:
                break
            await asyncio.sleep(0.01)
        row = await self.db.fetchone("SELECT status,result_json FROM tool_jobs WHERE id=?", (job_id,))
        require(row["status"] == "cancelled", "取消状态被后到结果覆盖")
        if scenario == "late_success":
            require(bool(json.loads(row["result_json"] or "{}")), "丢失了远端可能已成功的回执")
            require(any(event["type"] == "tool.late_result" for event in self.events), "未记录迟到结果")
        return {"status": row["status"], "late_receipt": bool(row["result_json"])}

    async def fault(self, case):
        self.reset()
        scenario, tool, cid = case["expected"]["scenario"], case["tool"], case["id"]
        attempts = 0
        started, release = asyncio.Event(), asyncio.Event()

        async def dispatch(name, args, task_id):
            nonlocal attempts
            if args.get("branch") == "fast":
                return {"ok": True, "branch": "fast"}
            attempts += 1
            if scenario == "parallel_progress":
                started.set()
                await release.wait()
                return {"ok": True}
            if scenario == "timeout":
                await asyncio.sleep(1)
            if scenario == "transient" and attempts > 1:
                return {"ok": True}
            return {"error": "injected service failure", "error_code": "INJECTED", "retryable": scenario == "transient"}

        self.handler = dispatch
        job = {"task_id": cid, "tool_name": tool, "arguments": {"case": cid}, "idempotency_key": cid,
               "timeout_seconds": 0.08 if scenario == "timeout" else 3,
               "max_retries": 1 if scenario == "transient" else 0}
        if scenario == "parallel_progress":
            slow = await self.queue.submit(**job)
            await asyncio.wait_for(started.wait(), 2)
            try:
                fast = await self.queue.execute(**{**job, "task_id": cid + "-fast", "arguments": {"branch": "fast"}, "idempotency_key": cid + "-fast"})
                row = await self.db.fetchone("SELECT status FROM tool_jobs WHERE id=?", (slow,))
                require(fast.get("ok") and row["status"] == "running", "独立快分支被慢分支阻塞")
            finally:
                release.set()
            await self.queue.wait(slow)
            return {"fast_finished_while_slow_running": True}
        first = await self.queue.submit(**job)
        result = await self.queue.wait(first)
        if scenario == "transient":
            require(result.get("ok") and attempts == 2, "瞬态错误重试不正确")
        else:
            require(result.get("queue_status") == "dead_letter" and attempts == 1, "错误未按预算进入死信队列")
        if scenario == "dependency_failure":
            dependent = await self.queue.execute(**{**job, "idempotency_key": cid + "-dependent", "dependencies": [first]})
            require(dependent.get("queue_status") == "dead_letter", "依赖失败没有阻断后继")
            require(attempts == 1, "依赖失败仍执行后继工具")
        return {"attempts": attempts, "result": result, "events": list(self.events)}
