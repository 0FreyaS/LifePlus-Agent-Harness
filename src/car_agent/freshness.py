"""Time boundaries for conversation context, user decisions and tool evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def recent(value: Any, seconds: float, now: datetime | None = None) -> bool:
    timestamp = parse_time(value)
    age = ((now or utcnow()) - timestamp).total_seconds() if timestamp else None
    return age is not None and -5 <= age < seconds


def expired(payload: dict, now: datetime | None = None) -> bool:
    now = now or utcnow()
    end, start = parse_time(payload.get("expires_at")), parse_time(payload.get("issued_at"))
    return end is None or end <= now or (start is not None and start > now + timedelta(seconds=5))


def stale_receipt(result: dict, now: datetime | None = None) -> bool:
    metadata = result.get("_freshness")
    return isinstance(metadata, dict) and expired(metadata, now)


@dataclass(frozen=True)
class FreshnessPolicy:
    context_seconds: float = 1800
    selection_seconds: float = 300
    approval_seconds: float = 120
    clarification_seconds: float = 1800
    weather_seconds: float = 300
    places_seconds: float = 300
    route_seconds: float = 120
    music_seconds: float = 1800

    @classmethod
    def from_settings(cls, settings):
        return cls(**{name: getattr(settings, "freshness_" + name) for name in cls.__dataclass_fields__})

    def tool_seconds(self, tool: str) -> float | None:
        return {
            "maps_weather": self.weather_seconds,
            "maps_search": self.places_seconds,
            "maps_local_search": self.places_seconds,
            "maps_place_detail": self.places_seconds,
            "maps_route": self.route_seconds,
            "search_music": self.music_seconds,
        }.get(tool)

    def receipt(self, tool: str, result: dict, observed: str | None = None) -> dict:
        ttl = self.tool_seconds(tool)
        if ttl is None:
            return result
        timestamp = parse_time(observed) if observed else utcnow()
        timestamp = timestamp or utcnow()
        return {**result, "_freshness": {
            "source": tool, "issued_at": timestamp.isoformat(),
            "expires_at": (timestamp + timedelta(seconds=ttl)).isoformat(),
            "ttl_seconds": ttl, "scope": "retrieval_receipt_not_provider_truth",
        }}

    def interrupt(self, identifier: str, value: dict, state: dict) -> dict:
        now = utcnow()
        ttl = {"selection": self.selection_seconds, "approval": self.approval_seconds}.get(
            value.get("kind"), self.clarification_seconds)
        end = now + timedelta(seconds=ttl)
        if value.get("kind") == "selection":
            for result in (state.get("tool_result", {}), state.get("tool_result", {}).get("route_result", {})):
                evidence_end = parse_time(result.get("_freshness", {}).get("expires_at"))
                if evidence_end:
                    end = min(end, evidence_end)
        return {**value, "id": identifier, "issued_at": now.isoformat(), "expires_at": end.isoformat()}


def lifecycle_lock(app, task: dict):
    import asyncio

    return app.state.locks.setdefault("lifecycle:" + (task.get("parent_task_id") or task["id"]), asyncio.Lock())
