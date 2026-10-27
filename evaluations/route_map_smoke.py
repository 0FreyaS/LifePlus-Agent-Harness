"""Live HTTP/model/MCP route-map smoke; creates a normal showcase conversation."""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import httpx


async def run(base_url: str, output: Path, query: str = "从杭州东站步行到杭州西湖怎么走", expected_mode: str = "walking"):
    output.mkdir(parents=True, exist_ok=False)
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": "demo-user"}, timeout=20, trust_env=False) as client:
        ready_until = monotonic() + 15
        while True:
            try:
                response = await client.get("/api/health")
                response.raise_for_status()
                break
            except (httpx.ConnectError, httpx.HTTPStatusError):
                if monotonic() >= ready_until:
                    raise
                await asyncio.sleep(0.25)
        async def get(path):
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

        response = await client.post("/api/sessions")
        response.raise_for_status()
        session = response.json()["id"]
        response = await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": "live", "request_id": str(uuid.uuid4())})
        response.raise_for_status()
        task_id = response.json()["task_id"]
        started, interactions = monotonic(), []
        while monotonic() - started < 150:
            task = await get(f"/api/tasks/{task_id}")
            if task["status"] == "completed":
                break
            if task["status"] in {"failed", "cancelled", "partial", "interrupted"}:
                raise RuntimeError(task.get("answer") or task["status"])
            pending = task.get("interrupt") or {}
            if task["status"] == "waiting_user":
                if pending.get("kind") != "selection" or not pending.get("candidates"):
                    raise RuntimeError("Unexpected clarification; smoke does not invent missing slots")
                # Select an actually offered candidate; do not inject tool results or graph state.
                interactions.append({"interrupt_id": pending["id"], "selected_candidate": pending["candidates"][0]})
                response = await client.post(f"/api/tasks/{task_id}/resume", json={"interrupt_id": pending["id"], "action": "answer", "value": {"index": 1}})
                response.raise_for_status()
            await asyncio.sleep(0.25)
        else:
            raise TimeoutError("Route showcase exceeded 150 seconds")
        elapsed_ms = round((monotonic() - started) * 1000)
        route = await get(f"/api/tasks/{task_id}/route")
        assert route["geometry_available"] and len(route["geometry"]["coordinates"]) > 2
        assert route["travel_mode"] == expected_mode and route["distance_m"] > 0
        assert route["default_travel_mode"] == expected_mode
        assert route["mode_choices"][0]["travel_mode"] == expected_mode and route["mode_choices"][0]["is_default"]
        assert {choice["travel_mode"] for choice in route["mode_choices"]} == {"driving", "walking", "bicycling"}
        summary = await get(f"/api/sessions/{session}/routes")
        assert any(item["job_id"] == route["job_id"] for item in summary)
        images = []
        for name, zoom, large in (("route-overview", 0, False), ("route-zoomed-out", -1, False), ("route-detail", 0, True)):
            response = await client.get(f"/api/tasks/{task_id}/route/map", params={"job_id": route["job_id"], "zoom": zoom, "large": str(large).lower()})
            response.raise_for_status()
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
            dimensions = struct.unpack(">II", response.content[16:24])
            assert dimensions == ((1000, 600) if large else (800, 400))
            (output / f"{name}.png").write_bytes(response.content)
            images.append({"name": name, "dimensions": dimensions, "bytes": len(response.content)})
        record = {"tested_at": datetime.now(timezone.utc).isoformat(), "task_id": task_id, "session_id": session,
                  "model": (await get("/api/health"))["model_name"], "mode": "live", "query": query, "elapsed_ms": elapsed_ms,
                  "interactions": interactions, "route": route, "images": images,
                  "chat_path": f"/chat/{session}", "route_path": f"/routes/{task_id}?job={route['job_id']}",
                  "browser_visual_qa": False}
        (output / "report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"completed": True, "elapsed_ms": elapsed_ms, "default_mode": route["default_travel_mode"], "geometry_points": len(route["geometry"]["coordinates"]),
                          "images": images, "chat_path": record["chat_path"], "route_path": record["route_path"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--query", default="从杭州东站步行到杭州西湖怎么走")
    parser.add_argument("--expected-mode", choices=["driving", "walking", "bicycling"], default="walking")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.output, args.query, args.expected_mode))


if __name__ == "__main__":
    main()
