"""Real-model API workflows, isolated by a fresh validation user on each run."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


async def run_checks(base_url: str) -> dict:
    owner = "validation-" + uuid.uuid4().hex[:12]
    cases = []
    resources = {}
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": owner}, timeout=30) as client:
        for attempt in range(60):
            try:
                response = await client.get("/api/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if attempt < 59:
                await asyncio.sleep(0.5)

        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        async def session():
            return (await request("POST", "/api/sessions"))["id"]

        async def submit(session_id, message):
            return (await request("POST", f"/api/sessions/{session_id}/messages", json={
                "message": message, "mode": "live", "request_id": str(uuid.uuid4())}))["task_id"]

        async def settled(task_id):
            for _ in range(600):
                task = await request("GET", f"/api/tasks/{task_id}")
                if task["status"] not in {"queued", "running"}:
                    return task
                await asyncio.sleep(0.15)
            raise TimeoutError("Task did not reach a user interrupt or terminal state")

        async def resume(task, action, value=None):
            return await request("POST", f"/api/tasks/{task['id']}/resume", json={
                "interrupt_id": task["interrupt"]["id"], "action": action, "value": value or {}})

        async def check(name, operation):
            started = perf_counter()
            try:
                evidence = await asyncio.wait_for(operation(), timeout=110)
                case = {"case": name, "passed": True, "evidence": evidence}
            except Exception as exc:
                case = {"case": name, "passed": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            case["elapsed_seconds"] = round(perf_counter() - started, 3)
            cases.append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)

        async def health_and_catalog():
            health = await request("GET", "/api/health")
            tools = await request("GET", "/api/tools")
            assert health["model_configured"] and health["multi_task"] and health["response_streaming"], health
            from .tools import TOOL_CATALOG
            assert len(tools) == len(TOOL_CATALOG)
            return {"model": health["model_name"], "tools": len(tools), "checkpoint": health["checkpoint_backend"]}

        async def memory_workflow():
            first, second = await session(), await session()
            await request("PUT", f"/api/sessions/{first}/memories/transport", json={"value": "公共交通优先"})
            values = await request("GET", f"/api/sessions/{second}/memories")
            assert any(item["key"] == "transport" and item["value"] == "公共交通优先" for item in values)
            denied = await client.get(f"/api/sessions/{first}/memories", headers={"X-Demo-User": owner + "-other"})
            assert denied.status_code == 404
            return {"cross_session_preference": True, "other_user_status": denied.status_code}

        async def weather_clarification():
            session_id = await session()
            task_id = await submit(session_id, "请查询天气")
            task = await settled(task_id)
            resources["weather_task"] = task_id
            assert task["status"] == "waiting_user" and task["interrupt"]["field"] == "city", task
            await resume(task, "answer", {"text": "无锡"})
            task = await settled(task_id)
            assert task["status"] == "completed", task
            events = await request("GET", f"/api/tasks/{task_id}/trace")
            result = next(event["result"] for event in events if event["type"] == "tool.completed")
            assert result["source"] == "高德天气" and not result.get("simulated"), result
            assert any(event["type"] == "answer.delta" for event in events)
            return {"task_id": task_id, "field": "city", "source": result["source"], "status": task["status"]}

        async def rejected_write():
            task_id = await submit(await session(), "请保存一个行程，标题是验收测试待拒绝行程")
            task = await settled(task_id)
            assert task["status"] == "waiting_user" and task["interrupt"]["kind"] == "approval", task
            await resume(task, "reject")
            task = await settled(task_id)
            assert task["status"] == "cancelled", task
            assert (await request("GET", "/api/life/state"))["itineraries"] == []
            return {"task_id": task_id, "status": task["status"], "saved_items": 0}

        async def confirmed_write():
            session_id = await session()
            task_id = await submit(session_id, "请保存一个行程，标题是验收测试无锡一日游")
            task = await settled(task_id)
            assert task["status"] == "waiting_user" and task["interrupt"]["kind"] == "approval", task
            pending = task["interrupt"]
            await resume(task, "approve")
            task = await settled(task_id)
            assert task["status"] == "completed", task
            duplicate = await client.post(f"/api/tasks/{task_id}/resume", json={
                "interrupt_id": pending["id"], "action": "approve", "value": {}})
            assert duplicate.status_code == 409
            items = (await request("GET", "/api/life/state"))["itineraries"]
            assert len(items) == 1, items
            events = await request("GET", f"/api/tasks/{task_id}/trace")
            writes = [event for event in events if event["type"] == "tool.completed" and event.get("tool_name") == "save_itinerary"]
            assert len(writes) == 1 and not writes[0]["result"].get("simulated"), writes
            resources.update(write_task=task_id, write_session=session_id, itinerary_id=items[0]["id"])
            return {"task_id": task_id, "saved_items": len(items), "duplicate_confirm_status": duplicate.status_code}

        async def trace_review_checkpoint():
            task_id, session_id = resources["write_task"], resources["write_session"]
            events = await request("GET", f"/api/tasks/{task_id}/trace")
            sequences = [event["seq"] for event in events]
            assert sequences == sorted(set(sequences))
            checkpoints = await request("GET", f"/api/sessions/{session_id}/checkpoints")
            assert checkpoints and all(point.get("thread_id") for point in checkpoints)
            review = await request("PUT", f"/api/tasks/{task_id}/review", json={
                "is_bad_case": False, "category": "other", "note": "自动化验收：确认、执行和重复确认拦截正常"})
            assert review["is_bad_case"] is False
            assert (await request("GET", f"/api/tasks/{task_id}/review"))["note"] == review["note"]
            listed = await request("GET", "/api/tasks?review_state=reviewed&q=验收测试")
            assert any(item["id"] == task_id for item in listed)
            return {"events": len(events), "checkpoints": len(checkpoints), "review_roundtrip": True}

        for name, operation in [("health_and_catalog", health_and_catalog), ("memory_cross_session_and_isolation", memory_workflow),
                                ("live_weather_clarification", weather_clarification), ("live_write_rejected", rejected_write),
                                ("live_write_confirmed_once", confirmed_write), ("trace_review_checkpoint", trace_review_checkpoint)]:
            await check(name, operation)
    return {"tested_at": datetime.now(timezone.utc).isoformat(), "transport": "live HTTP API",
            "user_id": owner, "resources": resources, "passed": sum(case["passed"] for case in cases),
            "total": len(cases), "cases": cases,
            "data_policy": "Fresh validation user only; one labelled test itinerary is retained for audit. No existing user data is changed."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("data/http-workflow-acceptance-latest.json"))
    args = parser.parse_args()
    report = asyncio.run(run_checks(args.url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
