import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from car_agent.persistence import Database
from car_agent.reminders import ReminderScheduler
from car_agent.tools import LifeTools


@pytest.mark.asyncio
async def test_actual_itinerary_storage_is_idempotent_and_rejects_changed_parameters(tmp_path):
    path = str(tmp_path / "tools.sqlite3")
    db = Database(path)
    await db.open()
    try:
        tools = LifeTools(path)
        args = {"user_id": "alice", "title": "西湖散步", "idempotency_key": "one",
                "details": {"destination": "西湖"}}
        first, duplicate = await asyncio.gather(*(tools.call("save_itinerary", args) for _ in range(2)))
        assert first["ok"] and duplicate["ok"]
        assert first["item_id"] == duplicate["item_id"]
        stored = await tools.call("list_itineraries", {"user_id": "alice"})
        assert len(stored["items"]) == 1
        conflict = await tools.call("save_itinerary", {**args, "title": "不同地点"})
        assert conflict["error_code"] == "IDEMPOTENCY_CONFLICT"
        assert conflict["retryable"] is False
        isolated = await tools.call("list_itineraries", {"user_id": "bob"})
        assert isolated["items"] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_actual_reminder_storage_scheduler_and_cancellation(tmp_path):
    path = str(tmp_path / "reminders.sqlite3")
    db = Database(path)
    await db.open()
    tools = LifeTools(path)
    try:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        args = {"user_id": "alice", "title": "带伞", "remind_at": future, "idempotency_key": "due"}
        due = await tools.call("create_reminder", args)
        cancel = await tools.call("create_reminder", {**args, "idempotency_key": "cancel"})
        unauthorized = await tools.call("cancel_reminder", {"user_id": "bob", "reminder_id": due["reminder_id"]})
        assert unauthorized["error_code"] == "NOT_FOUND"
        cancelled = await tools.call("cancel_reminder", {"user_id": "alice", "reminder_id": cancel["reminder_id"]})
        assert cancelled["status"] == "cancelled"
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await db.execute("UPDATE reminders SET remind_at=?", (past,))
        scheduler = ReminderScheduler(db)
        assert await scheduler.tick() == 1
        assert await ReminderScheduler(db).tick() == 0
        notifications = await db.fetchall("SELECT * FROM notifications")
        assert len(notifications) == 1
        assert notifications[0]["reminder_id"] == due["reminder_id"]
        delivered = await tools.call("cancel_reminder", {"user_id": "alice", "reminder_id": due["reminder_id"]})
        assert delivered["error_code"] == "ALREADY_DELIVERED"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("maps_search", {"keywords": "   "}),
    ("maps_route", {"origin": "杭州", "destination": "上海", "travel_mode": "plane"}),
    ("search_music", {"keyword": "周杰伦", "limit": 0}),
    ("save_itinerary", {"user_id": "alice", "title": "出行"}),
    ("create_reminder", {"user_id": "alice", "title": "起床", "remind_at": "明天", "idempotency_key": "bad"}),
    ("create_reminder", {"user_id": "alice", "title": "起床", "remind_at": "2020-01-01T09:00:00+08:00", "idempotency_key": "past"}),
])
async def test_invalid_tool_parameters_fail_without_simulated_success(tmp_path, name, arguments):
    result = await LifeTools(str(tmp_path / "tools.sqlite3")).call(name, arguments)
    assert result["ok"] is False
    assert result["error_code"] == "INVALID_ARGUMENT"
    assert result["retryable"] is False
    assert "simulated" not in result
