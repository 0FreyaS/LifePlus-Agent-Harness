"""Verify route mode changes and actual provider alternatives via the public API."""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time
import uuid
from pathlib import Path

import httpx


async def run(base_url: str, task_id: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    cases = []
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": "demo-user"}, timeout=25, trust_env=False) as client:
        response = await client.get(f"/api/tasks/{task_id}/route")
        response.raise_for_status()
        source = response.json()
        for mode in ("walking", "driving", "bicycling"):
            started = time.monotonic()
            case = {"travel_mode": mode, "passed": False}
            try:
                payload = {"job_id": source["job_id"], "travel_mode": mode, "request_id": str(uuid.uuid4())}
                response = await client.post(f"/api/tasks/{task_id}/route/replan", json=payload)
                response.raise_for_status()
                new_id = response.json()["task_id"]
                case["task_id"] = new_id
                duplicate = await client.post(f"/api/tasks/{task_id}/route/replan", json=payload)
                duplicate.raise_for_status()
                assert duplicate.json()["task_id"] == new_id and duplicate.json()["duplicate"]
                while time.monotonic() - started < 65:
                    response = await client.get(f"/api/tasks/{new_id}")
                    response.raise_for_status()
                    task = response.json()
                    if task["status"] in {"completed", "failed", "cancelled", "interrupted", "waiting_user"}:
                        break
                    await asyncio.sleep(0.25)
                assert task["status"] == "completed", task.get("answer") or task["status"]
                response = await client.get(f"/api/tasks/{new_id}/route")
                response.raise_for_status()
                route = response.json()
                assert route["travel_mode"] == mode
                assert route["origin"] == source["origin"] and route["destination"] == source["destination"]
                assert route["geometry_available"] and route["source"] == "高德地图"
                images = []
                # The first and last actual alternatives exercise switching without inventing a path.
                options = [route["options"][0]]
                if len(route["options"]) > 1:
                    options.append(route["options"][-1])
                for option in options:
                    params = {"job_id": route["job_id"], "option": option["option_id"]}
                    selected = await client.get(f"/api/tasks/{new_id}/route", params=params)
                    selected.raise_for_status()
                    assert selected.json()["duration_s"] == option["duration_s"]
                    image = await client.get(f"/api/tasks/{new_id}/route/map", params=params)
                    image.raise_for_status()
                    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
                    assert struct.unpack(">II", image.content[16:24]) == (800, 400)
                    filename = f"{mode}-{option['option_id']}.png"
                    (output / filename).write_bytes(image.content)
                    images.append(filename)
                trace = (await client.get(f"/api/tasks/{new_id}/trace")).json()
                tool_events = [event for event in trace if event["type"] == "tool.completed"]
                assert len(tool_events) == 1 and tool_events[0]["tool_name"] == "maps_route"
                assert not tool_events[0]["result"].get("simulated")
                assert not any(event["type"].startswith("model.") for event in trace)
                case.update(passed=True, route=route, images=images, planning_seconds=round(time.monotonic() - started, 3))
            except (httpx.HTTPError, AssertionError, KeyError) as exc:
                case["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
            cases.append(case)
            print(json.dumps({key: value for key, value in case.items() if key != "route"}, ensure_ascii=False), flush=True)
        original = (await client.get(f"/api/tasks/{task_id}/route")).json()
        assert original["job_id"] == source["job_id"] and original["travel_mode"] == source["travel_mode"]
    report = {"mode": "live HTTP, MCP and AMap; no model calls for explicit mode switches", "browser_visual_qa": False,
              "source_task_id": task_id, "passed": sum(case["passed"] for case in cases), "total": len(cases), "cases": cases}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args.base_url, args.task_id, args.output))
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
