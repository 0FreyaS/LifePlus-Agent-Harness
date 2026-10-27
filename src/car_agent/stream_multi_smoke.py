"""Live HTTP/SSE acceptance checks; requires a running configured LifePlus server."""
import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


async def scenario(base_url: str, query: str, multi: bool) -> dict:
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": "stream-acceptance"}, timeout=120) as client:
        response = await client.post("/api/sessions")
        response.raise_for_status()
        session_id = response.json()["id"]
        started = perf_counter()
        response = await client.post(f"/api/sessions/{session_id}/messages", json={
            "message": query, "mode": "live", "request_id": str(uuid.uuid4())})
        response.raise_for_status()
        task_id = response.json()["task_id"]
        pending = None
        weather_completed = False
        weather_finished = False
        resumed = False
        first_delta = None
        delta_count = 0
        streams: dict[str, dict] = {}
        sources = []
        final = ""
        final_status = ""
        first_complete = None
        async with client.stream("GET", f"/api/tasks/{task_id}/events") as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event["type"]
                elapsed = round(perf_counter() - started, 3)
                if kind == "answer.delta":
                    first_delta = first_delta if first_delta is not None else elapsed
                    delta_count += 1
                    key = event.get("stream_id", task_id)
                    stream = streams.setdefault(key, {"first_delta_seconds": elapsed, "delta_count": 0})
                    stream["delta_count"] += 1
                if kind == "tool.completed":
                    result = event.get("result", {})
                    assert not result.get("simulated"), "Live request used simulated tool data"
                    sources.append({"tool": event.get("tool_name"), "source": result.get("source")})
                if kind == "subtask.interrupt.required":
                    assert event["kind"] == "selection", event
                    pending = event
                if kind in {"subtask.task.completed", "subtask.task.failed", "subtask.task.cancelled"}:
                    first_complete = first_complete if first_complete is not None else elapsed
                    task = (await client.get("/api/tasks/" + event["child_task_id"])).json()
                    if task["domain"] == "weather":
                        weather_finished = True
                        weather_completed = kind == "subtask.task.completed"
                if kind in {"answer.completed", "subtask.answer.completed"}:
                    key = event.get("child_task_id") or task_id
                    if key in streams:
                        streams[key]["completed_seconds"] = elapsed
                if pending and weather_finished and not resumed:
                    resumed_response = await client.post(f"/api/tasks/{pending['child_task_id']}/resume", json={
                        "interrupt_id": pending["id"], "action": "answer", "value": {"index": 1}})
                    resumed_response.raise_for_status()
                    resumed = True
                    print(f"weather finished (success={weather_completed}) before music selection was resumed", flush=True)
                if kind in {"task.completed", "task.partial", "task.failed", "task.cancelled"}:
                    final_status = kind[5:]
                    final = event.get("answer", "")
                    break
        task = (await client.get("/api/tasks/" + task_id)).json()
        passed = (final_status == "completed" and delta_count > 1 and first_delta is not None
                  and all(item["first_delta_seconds"] < item.get("completed_seconds", float("inf")) for item in streams.values()))
        if multi:
            passed = passed and resumed and weather_completed and len(task.get("children", [])) == 2
            passed = passed and all(child["status"] == "completed" for child in task["children"])
            passed = passed and {source["source"] for source in sources} >= {"高德天气", "QQ音乐"}
        result = {"query": query, "passed": passed, "task_id": task_id, "session_id": session_id,
                  "delta_count": delta_count, "first_delta_seconds": first_delta,
                  "first_subtask_complete_seconds": first_complete, "total_seconds": round(perf_counter() - started, 3),
                  "weather_completed_while_music_waited": weather_completed and resumed if multi else None,
                  "streams": streams, "sources": sources, "answer": final, "status": final_status}
        print(json.dumps({key: value for key, value in result.items() if key not in {"answer", "streams"}}, ensure_ascii=False), flush=True)
        return result


async def run(base_url: str) -> dict:
    cases = []
    for query, multi in [("请简短介绍你能提供的服务", False), ("查询无锡今天的天气，同时搜索陶喆的歌曲", True)]:
        try:
            cases.append(await asyncio.wait_for(scenario(base_url, query, multi), timeout=100))
        except (AssertionError, TimeoutError, httpx.HTTPError) as exc:
            cases.append({"query": query, "passed": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return {"tested_at": datetime.now(timezone.utc).isoformat(), "mode": "live HTTP SSE",
            "passed": sum(bool(case["passed"]) for case in cases), "total": len(cases), "cases": cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("data/stream-multi-acceptance-latest.json"))
    args = parser.parse_args()
    report = asyncio.run(run(args.url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
