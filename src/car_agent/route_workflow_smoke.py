"""Exercise the original cross-city failure through the live model and HTTP workflow."""
import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


async def run(base_url: str) -> dict:
    started = perf_counter()
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "query": "导航到南京市东南大学四牌楼校区", "origin_reply": "从无锡市人民医院出发",
              "interrupts": [], "transport": "live model / HTTP / MCP"}
    owner = "route-validation-" + uuid.uuid4().hex[:12]
    async with httpx.AsyncClient(base_url=base_url, headers={"X-Demo-User": owner}, timeout=30) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        try:
            for _ in range(60):
                try:
                    health = await request("GET", "/api/health")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.5)
            else:
                raise RuntimeError("Server did not become ready")
            assert health["model_configured"]
            report["model"] = health["model_name"]
            session = await request("POST", "/api/sessions")
            submitted = await request("POST", f"/api/sessions/{session['id']}/messages", json={
                "message": report["query"], "mode": "live", "request_id": str(uuid.uuid4())})
            task_id = submitted["task_id"]
            report.update(task_id=task_id, session_id=session["id"])
            resumed = set()
            for _ in range(600):
                task = await request("GET", f"/api/tasks/{task_id}")
                if task["status"] in {"completed", "failed", "partial", "cancelled", "interrupted"}:
                    break
                if task["status"] == "waiting_user":
                    pending = task["interrupt"]
                    assert pending["id"] not in resumed, "Unexpected repeated interrupt"
                    resumed.add(pending["id"])
                    report["interrupts"].append({"kind": pending["kind"], "field": pending.get("field")})
                    if pending["kind"] == "clarification" and pending.get("field") == "origin":
                        value = {"text": report["origin_reply"]}
                    elif pending["kind"] == "selection":
                        candidates = pending["candidates"]
                        index = next((index for index, item in enumerate(candidates, 1)
                                      if "东南大学" in item.get("name", "") and "四牌楼" in item.get("name", "")), None)
                        assert index, "Expected campus was not present in the actual candidates"
                        value = {"index": index}
                        report["selected"] = candidates[index - 1]["name"]
                    else:
                        raise AssertionError("Unexpected interrupt: " + str(pending.get("kind")))
                    await request("POST", f"/api/tasks/{task_id}/resume", json={
                        "interrupt_id": pending["id"], "action": "answer", "value": value})
                await asyncio.sleep(0.15)
            else:
                raise TimeoutError("Navigation workflow exceeded the test deadline")
            assert task["status"] == "completed", task.get("answer")
            events = await request("GET", f"/api/tasks/{task_id}/trace")
            route_event = next(event for event in events if event["type"] == "tool.completed" and event.get("tool_name") == "maps_route")
            result = route_event["result"]
            assert result["ok"] and not result.get("simulated") and result["distance_m"] > 0
            lng, lat = map(float, result["origin"].split(","))
            assert 120 < lng < 121 and 31 < lat < 32, "Origin was not resolved in Wuxi"
            assert any(item["kind"] == "clarification" and item["field"] == "origin" for item in report["interrupts"])
            report.update(passed=True, status=task["status"], route=result, answer=task["answer"])
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
            if report.get("task_id"):
                await client.post(f"/api/tasks/{report['task_id']}/cancel")
    report["elapsed_seconds"] = round(perf_counter() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--output", type=Path, default=Path("data/route-workflow-regression-latest.json"))
    args = parser.parse_args()
    report = asyncio.run(run(args.url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"route", "answer"}}, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
