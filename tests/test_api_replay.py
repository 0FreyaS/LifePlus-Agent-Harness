import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from car_agent.api.app import app, settings


@pytest.mark.asyncio
async def test_api_can_fork_and_replay_a_persisted_checkpoint(tmp_path):
    original_path = settings.database_path
    settings.database_path = str(tmp_path / "api.sqlite3")
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                headers = {"X-Demo-User": "replay-test"}
                session = (await client.post("/api/sessions", headers=headers)).json()
                session_id = session["id"]
                submitted = await client.post(f"/api/sessions/{session_id}/messages", headers=headers,
                                              json={"message": "杭州天气怎么样", "mode": "demo"})
                task_id = submitted.json()["task_id"]

                async def wait_for_task(identifier):
                    for _ in range(80):
                        task = (await client.get(f"/api/tasks/{identifier}", headers=headers)).json()
                        if task["status"] not in {"queued", "running"}:
                            return task
                        await asyncio.sleep(0.05)
                    raise AssertionError("task did not finish")

                original = await wait_for_task(task_id)
                assert original["status"] == "completed"
                checkpoints = (await client.get(f"/api/sessions/{session_id}/checkpoints", headers=headers)).json()
                replayable = next(point for point in checkpoints if point["next"])
                branch_response = await client.post(
                    f"/api/sessions/{session_id}/checkpoints/{replayable['checkpoint_id']}/replay",
                    headers=headers,
                )
                assert branch_response.status_code == 200
                branch = await wait_for_task(branch_response.json()["task_id"])
                assert branch["status"] == "completed"
                assert "离线演示数据" in branch["answer"]

                events = (await client.get(f"/api/tasks/{branch['id']}/trace", headers=headers)).json()
                assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
                assert any(event.get("replay_from") == replayable["checkpoint_id"] for event in events)

                saved = await client.put(
                    f"/api/tasks/{branch['id']}/review", headers=headers,
                    json={"is_bad_case": True, "category": "intent", "root_cause": "测试归因",
                          "expected_answer": "先澄清", "note": "回归数据"},
                )
                assert saved.status_code == 200
                assert saved.json()["is_bad_case"] is True
                assert (await client.get(f"/api/tasks/{branch['id']}/review", headers=headers)).json()["root_cause"] == "测试归因"
                listed = await client.get("/api/tasks?review_state=bad_case&q=杭州", headers=headers)
                assert listed.status_code == 200
                assert any(item["id"] == branch["id"] and item["domain"] == "weather" for item in listed.json())
                summary = (await client.get("/api/analytics/summary", headers=headers)).json()
                assert summary["bad_cases"] == 1
                assert summary["reviewed"] == 1
                denied = await client.get(f"/api/tasks/{branch['id']}/review", headers={"X-Demo-User": "other-user"})
                assert denied.status_code == 404
    finally:
        settings.database_path = original_path


@pytest.mark.asyncio
async def test_real_reminder_delivery_and_notification_ownership(tmp_path):
    original_path = settings.database_path
    settings.database_path = str(tmp_path / "notifications.sqlite3")
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                await app.state.context.mcp.open()
                args = {"user_id": "notify-user", "title": "站内通知测试",
                        "remind_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                        "idempotency_key": "notification-test"}
                created = await app.state.context.mcp.call("create_reminder", args)
                assert created["ok"] is True, created
                for _ in range(50):
                    result = await client.get("/api/notifications", headers={"X-Demo-User": "notify-user"})
                    if result.json():
                        break
                    await asyncio.sleep(0.1)
                notifications = result.json()
                assert len(notifications) == 1
                notification = notifications[0]
                assert notification["reminder_id"] == created["reminder_id"]
                hidden = await client.get("/api/notifications", headers={"X-Demo-User": "other-user"})
                assert hidden.json() == []
                forbidden = await client.post(f"/api/notifications/{notification['id']}/read",
                                               headers={"X-Demo-User": "other-user"})
                assert forbidden.status_code == 404
                read = await client.post(f"/api/notifications/{notification['id']}/read",
                                         headers={"X-Demo-User": "notify-user"})
                assert read.status_code == 200
                assert (await client.get("/api/notifications", headers={"X-Demo-User": "notify-user"})).json()[0]["read_at"]
                from car_agent.tools import TOOL_CATALOG
                assert len((await client.get("/api/tools")).json()) == len(TOOL_CATALOG)
    finally:
        settings.database_path = original_path
