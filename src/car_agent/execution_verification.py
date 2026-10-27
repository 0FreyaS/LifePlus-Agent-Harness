"""Runtime evidence checks, not a substitute for a user-goal evaluation oracle."""
from __future__ import annotations

from .freshness import stale_receipt

def verify_execution(state: dict) -> dict:
    domain, action = state.get("domain"), state.get("action")
    result, operation = state.get("tool_result", {}), state.get("operation_result", {})
    slots = state.get("slots", {})
    live = state.get("mode") == "live"
    problems = []
    positive_result = True
    evidence = []
    if state.get("status") in {"failed", "cancelled", "waiting_user"}:
        return {"contract_valid": False, "positive_result": False, "enforce_failure": False,
                "scope": "tool_evidence", "problems": [state["status"]], "evidence": []}
    if live and (result.get("simulated") or operation.get("simulated")):
        problems.append("live_result_is_simulated")
    if stale_receipt(result) or stale_receipt(result.get("route_result") or {}):
        problems.append("stale_tool_receipt")
    if result.get("error") or result.get("ok") is False or operation.get("error") or operation.get("ok") is False:
        problems.append("tool_reported_failure")
    if domain == "weather":
        if not all(result.get(key) for key in ("city", "date", "day_weather")):
            problems.append("weather_receipt_incomplete")
        else:
            evidence.append("weather_fields")
    elif domain in {"navigation", "local_life", "music"}:
        items = result.get("items") or ([result["item"]] if result.get("item") else [])
        positive_result = bool(items)
        if items:
            index = int(slots.get("selection", 1)) - 1
            if not 0 <= index < len(items):
                problems.append("selected_candidate_missing")
            else:
                evidence.append("selected_candidate")
                if domain == "music" and live and not items[index].get("url"):
                    problems.append("song_link_missing")
                if domain == "local_life" and action == "local_detail" and items[index].get("id") != slots.get("poi_id"):
                    problems.append("poi_id_mismatch")
        if slots.get("route_requested"):
            route = result.get("route_result") or {}
            try:
                valid = float(route.get("distance_m", 0)) > 0 and float(route.get("duration_s", 0)) > 0
            except (ValueError, TypeError):
                valid = False
            if not valid or (live and not route.get("navigation_url")):
                problems.append("route_receipt_incomplete")
            else:
                evidence.append("route_receipt")
            if action == "navigation_route" and any(route.get(key) != slots.get(key) for key in ("origin", "destination", "travel_mode")):
                problems.append("replanned_route_mismatch")
            if action == "navigation_followup" and any(route.get(key) != slots.get(key) for key in ("destination", "travel_mode")):
                problems.append("conversation_route_mismatch")
    elif domain == "planning" and action in {"save_itinerary", "create_reminder", "cancel_reminder"}:
        expected = {"save_itinerary": "saved", "create_reminder": "scheduled", "cancel_reminder": "cancelled"}[action]
        if operation.get("status") != expected:
            problems.append("write_receipt_incomplete")
        if live and action in {"save_itinerary", "create_reminder"}:
            if not operation.get("item_id" if action == "save_itinerary" else "reminder_id"):
                problems.append("write_resource_id_missing")
        if not problems:
            evidence.append("write_receipt")
    elif domain == "planning":
        if not isinstance(result.get("items"), list):
            problems.append("list_receipt_incomplete")
        else:
            evidence.append("list_receipt")
    else:
        return {"contract_valid": True, "positive_result": bool(result.get("text")), "enforce_failure": False,
                "scope": "text_delivery_only", "problems": [], "evidence": ["text"] if result.get("text") else []}
    return {"contract_valid": not problems, "positive_result": positive_result,
            "enforce_failure": bool(problems), "scope": "tool_evidence",
            "problems": problems, "evidence": evidence}
