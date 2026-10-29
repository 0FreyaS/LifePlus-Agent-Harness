"""Opt-in authenticated HTTP/SSE acceptance of the deployed DeepSeek service."""
import argparse
import asyncio
import json
from pathlib import Path
from time import monotonic

import httpx


async def run(base_url, output):
    from car_agent.config import Settings
    from .jev_ab import attempts_from_events

    settings = Settings()
    if not settings.showcase_password:
        raise ValueError("Private showcase credentials required")
    auth = (settings.showcase_username, settings.showcase_password.get_secret_value())
    rows = []
    async with httpx.AsyncClient(base_url=base_url, auth=auth, timeout=90) as client:
        health = (await client.get("/api/health")).raise_for_status().json()
        assert health["model_name"] == "deepseek-flash"
        assert health["domain_route_provider"] == "jev_cascade"
        assert health["input_event_provider"] == settings.input_event_provider
        assert health["planning_gate_provider"] == "rules"
        for query in ("查询无锡今天天气", "介绍歌手陶喆"):
            session = (await client.post("/api/sessions")).raise_for_status().json()["id"]
            started = monotonic()
            result = (await client.post(f"/api/sessions/{session}/messages", json={"message": query, "mode": "live"})).raise_for_status().json()
            task_id = result["task_id"]
            events = []
            async with asyncio.timeout(90), client.stream("GET", f"/api/tasks/{task_id}/events") as stream:
                stream.raise_for_status()
                assert stream.headers["content-type"].startswith("text/event-stream")
                async for line in stream.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            task = (await client.get(f"/api/tasks/{task_id}")).raise_for_status().json()
            attempts = attempts_from_events(events)
            llm = [attempt for attempt in attempts if attempt.get("provider") != "typesafe"]
            assert task["status"] == "completed", task["status"]
            assert llm and all(attempt["model_name"] == "deepseek-flash" for attempt in llm)
            if "天气" in query:
                receipts = [event for event in events if event["type"] == "tool.completed" and event.get("tool_name") == "maps_weather"]
                assert receipts and all(not event.get("result", {}).get("error") for event in receipts)
            rows.append({"query": query, "task_id": task_id, "session_id": session,
                "status": task["status"], "elapsed_ms": round((monotonic()-started)*1000),
                "events": len(events), "model_attempts": [{key: event.get(key) for key in (
                    "stage", "model_name", "provider", "elapsed_ms", "usage")} for event in attempts]})
    evidence = {"health": health, "transport": "real_authenticated_http_sse", "cases": rows,
        "limitations": "Two post-deployment smoke requests, not a paired performance benchmark or factual-answer audit."}
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"model": health["model_name"], "passed": len(rows), "output": str(output)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:8088")
    parser.add_argument("--output", type=Path, default=Path("data/deepseek-rollout-smoke-20261029.json"))
    args = parser.parse_args()
    if args.run_live:
        asyncio.run(run(args.base_url, args.output))
    else:
        print("Prepared only; --run-live creates real sessions and calls paid providers.")


if __name__ == "__main__":
    main()
