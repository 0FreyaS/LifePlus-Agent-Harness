"""Capture real scheduler/trace evidence for the dependency and expanded graph views."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx

QUERY = (
    "请同时完成两件事：检索杭州东站这个车站地点的经纬度（不是饭店等商家检索）、查询无锡今天的天气。"
    "杭州东站的位置确定后，再以第一项已选地点坐标为起点，步行规划到杭州西湖的路线。"
    "第三项依赖第一项选中的地点坐标，不能猜起点；天气任务独立执行。"
)


async def capture(base_url: str) -> dict:
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": "demo-user"}, timeout=30, trust_env=False) as client:
        response = await client.post("/api/sessions")
        response.raise_for_status()
        session_id = response.json()["id"]
        response = await client.post(f"/api/sessions/{session_id}/messages", json={
            "message": QUERY, "mode": "live", "request_id": str(uuid.uuid4())})
        response.raise_for_status()
        task_id = response.json()["task_id"]
        snapshots, selections, handled = [], [], set()
        start = time.monotonic()
        while time.monotonic() - start < 150:
            response = await client.get(f"/api/tasks/{task_id}")
            response.raise_for_status()
            task = response.json()
            states = [(child["id"], child["status"]) for child in task.get("children", [])]
            if not snapshots or states != snapshots[-1]["states"]:
                snapshots.append({"seconds": round(time.monotonic() - start, 3), "states": states})
            for child in task.get("children", []):
                pending = child.get("interrupt") or {}
                if pending.get("kind") == "selection" and pending.get("id") not in handled:
                    # Select an actually offered candidate; never authorize writes or invent slots.
                    response = await client.post(f"/api/tasks/{child['id']}/resume", json={
                        "interrupt_id": pending["id"], "action": "answer", "value": {"index": 1}})
                    response.raise_for_status()
                    handled.add(pending["id"])
                    selections.append({"task_id": child["id"], "interrupt_id": pending["id"], "index": 1})
            if task["status"] in {"completed", "partial", "failed", "cancelled"} or any(
                child.get("interrupt", {}).get("kind") == "clarification" for child in task.get("children", [])):
                break
            await asyncio.sleep(0.5)
        response = await client.get(f"/api/tasks/{task_id}/trace")
        response.raise_for_status()
        events = response.json()
        children = task.get("children", [])
        plans = {json.loads(child["plan_json"])["id"]: child for child in children}
        dependencies, ordered = [], True
        for key, child in plans.items():
            for source in json.loads(child["plan_json"]).get("depends_on", []):
                predecessor = plans[source]
                done = next((event["seq"] for event in events if event.get("child_task_id") == predecessor["id"] and event["type"] == "subtask.task.completed"), None)
                begun = next((event["seq"] for event in events if event.get("child_task_id") == child["id"] and event["type"] == "subtask.task.started"), None)
                valid = done is not None and begun is not None and done < begun
                ordered = ordered and valid
                dependencies.append({"source": source, "target": key, "completion_before_start": valid})
        independent = sum(not json.loads(child["plan_json"]).get("depends_on") for child in children)
        return {"passed": task["status"] == "completed" and independent >= 2 and bool(dependencies) and ordered,
                "mode": "live HTTP; real model and MCP", "browser_visual_qa": False,
                "elapsed_seconds": round(time.monotonic() - start, 3), "task_id": task_id, "session_id": session_id,
                "independent_entries": independent, "dependencies": dependencies, "snapshots": snapshots,
                "candidate_selections": selections, "task": task, "events": events}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit("Choose a new output path; existing evidence is preserved.")
    report = asyncio.run(capture(args.base_url))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("passed", "elapsed_seconds", "task_id", "independent_entries", "dependencies")}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
