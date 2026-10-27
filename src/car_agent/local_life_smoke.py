"""Live local-life acceptance through the same APIs used by the discovery page."""
import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


async def run(base_url: str) -> dict:
    cases, tasks, resources = [], [], {}
    owner = "local-validation-" + uuid.uuid4().hex[:12]
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": owner}, timeout=30) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        async def settled(task_id):
            for _ in range(600):
                task = await request("GET", f"/api/tasks/{task_id}")
                if task["status"] not in {"queued", "running"}:
                    return task
                await asyncio.sleep(0.15)
            raise TimeoutError("Task did not settle")

        async def form(action, parameters):
            session = await request("POST", "/api/sessions")
            submitted = await request("POST", "/api/local-life/tasks", json={"session_id": session["id"],
                "action": action, "parameters": parameters, "mode": "live", "request_id": str(uuid.uuid4())})
            tasks.append(submitted["task_id"])
            return await settled(submitted["task_id"])

        async def resume(task, value):
            await request("POST", f"/api/tasks/{task['id']}/resume", json={
                "interrupt_id": task["interrupt"]["id"], "action": "answer", "value": value})
            return await settled(task["id"])

        async def result(task, tool):
            events = await request("GET", f"/api/tasks/{task['id']}/trace")
            value = next(event["result"] for event in reversed(events)
                         if event["type"] == "tool.completed" and event.get("tool_name") == tool)
            assert value["ok"] and not value.get("simulated"), value
            return value

        async def search_case(category, **constraints):
            task = await form("local_search", {"category": category, "anchor": "东南大学无锡国际校区",
                "city": "无锡市", "radius_m": 5000, **constraints})
            search = await result(task, "maps_local_search")
            assert search["items"] and all(item["availability"] != "reported_closed" for item in search["items"])
            selected = search["items"][0]
            if task["status"] == "waiting_user":
                assert task["interrupt"]["kind"] == "selection" and task["interrupt"].get("field") != "anchor"
                task = await resume(task, {"index": 1})
            assert task["status"] == "completed", task.get("answer")
            detail = await result(task, "maps_place_detail")
            assert detail["item"]["id"] == selected["id"]
            if category == "cinema":
                resources.update(venue=detail["item"], center=search["center"])
            return task, search

        async def ktv_filters():
            task, search = await search_case("ktv", min_rating=4, max_cost=100, sort_by="rating")
            assert all(item["rating"] >= 4 and item["cost"] <= 100 for item in search["items"])
            return {"task_id": task["id"], "matches": len(search["items"]), "category": "ktv", "constraints": search["constraints"]}

        async def cinema_details():
            task, search = await search_case("cinema")
            return {"task_id": task["id"], "selected_id": resources["venue"]["id"], "selected_name": resources["venue"]["name"],
                    "closed_excluded": search["excluded_closed_count"], "center": search["center"]}

        async def selected_route():
            task = await form("local_route", {"poi_id": resources["venue"]["id"], "anchor": resources["center"], "travel_mode": "walking"})
            assert task["status"] == "completed", task.get("answer")
            route = await result(task, "maps_route")
            assert route["distance_m"] > 0 and route["destination"] == resources["venue"]["location"]
            return {"task_id": task["id"], "poi_id": resources["venue"]["id"], "distance_m": route["distance_m"], "source": route["source"]}

        async def natural_clarification():
            session = await request("POST", "/api/sessions")
            submitted = await request("POST", f"/api/sessions/{session['id']}/messages", json={"message": "帮我找附近的KTV", "mode": "live"})
            tasks.append(submitted["task_id"])
            task = await settled(submitted["task_id"])
            assert task["status"] == "waiting_user" and task["interrupt"]["field"] == "anchor", task
            task = await resume(task, {"text": "东南大学无锡国际校区"})
            search = await result(task, "maps_local_search")
            assert search["category"] == "ktv"
            if task["status"] == "waiting_user":
                task = await resume(task, {"index": 1})
            assert task["status"] == "completed", task.get("answer")
            return {"task_id": task["id"], "clarified_field": "anchor", "source": search["source"]}

        async def concurrent_weather():
            session = await request("POST", "/api/sessions")
            submitted = await request("POST", f"/api/sessions/{session['id']}/messages", json={
                "message": "查询无锡今天的天气，同时在东南大学无锡国际校区附近找电影院", "mode": "live"})
            tasks.append(submitted["task_id"])
            task = await settled(submitted["task_id"])
            children = {child["domain"]: child for child in task["children"]}
            assert children["weather"]["status"] == "completed" and children["local_life"]["status"] == "waiting_user", children
            await resume(children["local_life"], {"index": 1})
            task = await settled(task["id"])
            assert task["status"] == "completed"
            return {"task_id": task["id"], "weather_completed_while_selection_waited": True, "children": len(task["children"])}

        for name, operation in [("ktv_strict_filters_and_detail", ktv_filters), ("cinema_selection_and_detail", cinema_details),
                                ("selected_venue_route", selected_route), ("natural_location_clarification", natural_clarification),
                                ("weather_and_local_life_independent", concurrent_weather)]:
            started = perf_counter()
            try:
                evidence = await asyncio.wait_for(operation(), timeout=110)
                case = {"case": name, "passed": True, "evidence": evidence}
            except Exception as exc:
                case = {"case": name, "passed": False, "error": f"{type(exc).__name__}: {str(exc)[:350]}"}
            case["elapsed_seconds"] = round(perf_counter() - started, 3)
            cases.append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)
        for task_id in tasks:
            task = await request("GET", f"/api/tasks/{task_id}")
            if task["status"] in {"waiting_user", "queued", "running"}:
                await client.post(f"/api/tasks/{task_id}/cancel")
    return {"tested_at": datetime.now(timezone.utc).isoformat(), "user_id": owner,
            "mode": "live model / HTTP / MCP", "passed": sum(case["passed"] for case in cases), "total": len(cases), "cases": cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("data/local-life-workflows-latest.json"))
    args = parser.parse_args()
    report = asyncio.run(run(args.url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
