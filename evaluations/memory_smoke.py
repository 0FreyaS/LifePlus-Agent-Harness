"""Explicit live conversation/memory check using a new synthetic user."""
import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

import httpx


async def capture(base: str) -> dict:
    started = time.monotonic()
    checks = {}
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30,
                                 headers={"X-Demo-User": "memory-live-" + str(uuid.uuid4())}) as client:
        health = (await client.get("/api/health")).json()
        if health.get("auth_mode") not in {"local", "demo"}:
            raise RuntimeError("Use an isolated local service, not a shared showcase account")
        if not health.get("model_configured"):
            raise RuntimeError("A real model must be configured")

        async def session():
            response = await client.post("/api/sessions")
            response.raise_for_status()
            return response.json()["id"]

        async def turn(identifier, query):
            response = await client.post(f"/api/sessions/{identifier}/messages", json={
                "message": query, "mode": "live", "request_id": str(uuid.uuid4())})
            response.raise_for_status()
            task = response.json()["task_id"]
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                result = await client.get(f"/api/tasks/{task}")
                result.raise_for_status()
                result = result.json()
                if result["status"] in {"completed", "failed", "partial", "waiting_user"}:
                    return result
                await asyncio.sleep(0.25)
            await client.post(f"/api/tasks/{task}/cancel")
            raise TimeoutError("Live memory turn exceeded 100 seconds")

        first = await session()
        original = "你好，这次对话的主题是周末去苏州散步。"
        one = await turn(first, original)
        two = await turn(first, "刚才我说去哪个城市散步？")
        checks["first_turn_completed"] = one["status"] == "completed"
        checks["live_model_uses_previous_turn"] = two["status"] == "completed" and "苏州" in two.get("answer", "")
        stored = await turn(first, "记住：出行偏好=步行")
        checks["explicit_preference_saved"] = stored["status"] == "completed"
        second = await session()
        recalled = await turn(second, "我之前保存的出行偏好是什么？")
        checks["live_model_reads_cross_session_preference"] = recalled["status"] == "completed" and "步行" in recalled.get("answer", "")
        lookup = await turn(first, "请原样返回我第一次输入的query")
        checks["exact_first_input"] = lookup.get("answer") == "本次会话的第一条输入是：\n" + original
        trace = await client.get(f"/api/tasks/{lookup['id']}/trace")
        trace.raise_for_status()
        checks["first_input_has_database_evidence"] = any(event["type"] == "conversation.history_retrieved" for event in trace.json())
        removed = await turn(second, "忘记偏好：出行偏好")
        preferences = await client.get(f"/api/sessions/{first}/memories")
        preferences.raise_for_status()
        checks["preference_deleted"] = removed["status"] == "completed" and not preferences.json()
        return {"passed": all(checks.values()), "checks": checks,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "scope": "real configured model over HTTP; synthetic isolated user; no browser verification"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output path")
    report = asyncio.run(capture(args.base_url))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
