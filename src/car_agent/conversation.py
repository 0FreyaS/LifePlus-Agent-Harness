"""Session-local references carry evidence, not reusable claims of tool success."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .freshness import expired, utcnow
from .route_geometry import point
from .travel_modes import explicit_travel_mode
from .user_events import selection_index

RELATIVE_ROUTE = re.compile(r"^(?:请|帮我)?(?:导航过去|导航到那里|带我过去|怎么去那里|(?:骑行|骑车|驾车|开车|步行|走路)过去)[吧。！？!?]*$")


async def save_reference(db, state: dict, freshness) -> None:
    domain = state.get("domain")
    if state.get("status") != "completed" or domain not in {"weather", "navigation", "local_life", "music"}:
        return
    result, slots = state.get("tool_result") or {}, state.get("slots") or {}
    if result.get("error"):
        return
    items = result.get("items") or ([result["item"]] if result.get("item") else [])
    index = slots.get("selection", 1)
    selected = items[index - 1] if type(index) is int and 1 <= index <= len(items) else None
    payload = {"source_task_id": state["task_id"], "domain": domain,
               "slots": {key: slots[key] for key in ("city", "forecast_date", "origin", "destination", "travel_mode", "keyword", "anchor") if key in slots}}
    if selected:
        payload["selected"] = {key: selected[key] for key in ("id", "name", "location", "address", "city", "singer") if key in selected}
    now = utcnow()
    seconds = freshness.context_seconds if domain == "weather" else freshness.selection_seconds
    deadline = now + timedelta(seconds=seconds)
    from .freshness import parse_time
    for receipt in (result, result.get("route_result") or {}):
        receipt_deadline = (receipt.get("_freshness") or {}).get("expires_at")
        parsed = parse_time(receipt_deadline)
        if parsed and domain != "weather":
            deadline = min(deadline, parsed)
    await db.execute(
        "INSERT INTO conversation_references VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id) DO UPDATE SET payload_json=excluded.payload_json,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
        (state["task_id"], state["user_id"], state["session_id"], domain,
         json.dumps(payload, ensure_ascii=False), deadline.isoformat(), now.isoformat()),
    )


async def load_references(db, owner: str, session: str) -> list[dict]:
    rows = await db.fetchall(
        "SELECT r.payload_json,r.expires_at FROM conversation_references r JOIN tasks t ON t.id=r.task_id "
        "WHERE r.user_id=? AND r.session_id=? AND t.user_id=r.user_id AND t.session_id=r.session_id "
        "AND t.status='completed' ORDER BY r.updated_at DESC LIMIT 8",
        (owner, session),
    )
    return [json.loads(row["payload_json"]) for row in rows if not expired(row)]


def followup_decision(query: str, references: list[dict]) -> dict | None:
    text = query.strip()
    day = re.fullmatch(r"(?:那)?(明天|后天)(?:的天气)?(?:呢|怎么样)?[？?。]*", text)
    if day and references and references[0].get("domain") == "weather":
        source = references[0]
        city = source.get("slots", {}).get("city")
        if city:
            today = datetime.now(timezone(timedelta(hours=8))).date()
            return {"domain": "weather", "action": "weather_lookup", "slots": {
                "city": city, "forecast_date": (today + timedelta(days=1 if day[1] == "明天" else 2)).isoformat()},
                "context_source_task_id": source["source_task_id"]}
    if RELATIVE_ROUTE.fullmatch(text):
        places = [ref for ref in references if ref.get("domain") in {"navigation", "local_life"}
                  and point((ref.get("selected") or {}).get("location"))]
        # Distinct recent destinations are ambiguous, including independent parallel tasks.
        locations = {(ref["selected"]["location"]) for ref in places}
        if len(locations) == 1:
            source, selected = places[0], places[0]["selected"]
            return {"domain": "navigation", "action": "navigation_followup", "slots": {
                "destination": selected["location"], "destination_name": selected.get("name", "目的地"),
                "travel_mode": explicit_travel_mode(text) or source.get("slots", {}).get("travel_mode") or "driving",
                "route_requested": True}, "context_source_task_id": source["source_task_id"]}
        return {"domain": "navigation", "action": "navigation_search", "slots": {}, "unresolved_reference": True}
    return None


def selection_reply(query: str, waiting: list[dict]) -> tuple[dict, int] | None:
    matches = []
    for task in waiting:
        pending = json.loads(task.get("interrupt_json") or "{}")
        if pending.get("kind") != "selection" or expired(pending):
            continue
        index = selection_index({"text": query}, pending.get("candidates", []))
        if index is not None:
            matches.append((task, index))
    return matches[0] if len(matches) == 1 else None


def city_reply(query: str, waiting: list[dict], cities: list[str]) -> dict | None:
    city = re.fullmatch(r"(?:我在|我想查|查)?\s*([^\s，。！？?]{1,20})[。！!]*", query.strip())
    if not city or city[1].removesuffix("市") not in cities:
        return None
    matches = [task for task in waiting if (pending := json.loads(task.get("interrupt_json") or "{}")).get("kind") == "clarification"
               and pending.get("field") == "city" and not expired(pending)]
    return matches[0] if len(matches) == 1 else None


def preference_command(query: str) -> tuple[str, str, str] | None:
    save = re.fullmatch(r"(?:请)?记住[：:]\s*([^=：:\n]{1,80})\s*[=：:]\s*(.{1,500})", query.strip())
    if save:
        key, value = save[1].strip(), save[2].strip()
        return ("put", key, value) if key and value else None
    delete = re.fullmatch(r"(?:请)?忘记偏好[：:]\s*(.{1,80})", query.strip())
    return ("delete", delete[1].strip(), "") if delete and delete[1].strip() else None


def asks_first_input(query: str) -> bool:
    return bool(re.search(r"第一次|第一(?:条|句|轮|个)|最(?:开始|初|早)", query)
                and re.search(r"输入|提问|问了|说了|说的|消息|query|第一个问题", query, re.I)
                and re.search(r"返回|原样|复述|回顾|记得|还记|告诉|什么|哪个|哪句|哪条|说了|问了", query))


async def first_input(db, owner: str, session: str) -> dict | None:
    return await db.fetchone(
        "SELECT id,query,created_at FROM tasks WHERE user_id=? AND session_id=? AND parent_task_id IS NULL "
        "ORDER BY created_at,id LIMIT 1", (owner, session))
