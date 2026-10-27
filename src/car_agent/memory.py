from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .persistence import Database
from .freshness import recent


def _message_dict(message: Any) -> dict[str, str] | None:
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "")
        content = message.get("content", "")
    else:
        role = str(getattr(message, "type", ""))
        content = getattr(message, "content", "")
    role = "assistant" if role in {"assistant", "ai"} else "user" if role in {"user", "human"} else ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = str(content).strip()
    return {"role": role, "content": text} if role and text else None


class MemoryRepository:
    """Redis-first session memory with MySQL or SQLite durable fallback."""

    def __init__(self, db: Database, redis_url: str | None = None, mysql_dsn: str | None = None,
                 history_messages: int = 12, history_characters: int = 12000,
                 context_seconds: float = 1800):
        self.db = db
        self.redis_url = redis_url
        self.mysql_dsn = mysql_dsn
        self.history_messages = history_messages
        self.history_characters = history_characters
        self.context_seconds = context_seconds
        self.redis: Any = None
        self.mysql: Any = None

    @property
    def cache_backend(self) -> str:
        return "redis" if self.redis else "local"

    @property
    def durable_backend(self) -> str:
        return "mysql" if self.mysql else "sqlite"

    async def open(self) -> None:
        if self.redis_url:
            from redis.asyncio import Redis

            self.redis = Redis.from_url(self.redis_url, decode_responses=True)
            await self.redis.ping()
        if self.mysql_dsn:
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import create_async_engine

            dsn = self.mysql_dsn
            if dsn.startswith("mysql://"):
                dsn = "mysql+asyncmy://" + dsn.removeprefix("mysql://")
            self.mysql = create_async_engine(dsn, pool_pre_ping=True)
            async with self.mysql.begin() as connection:
                await connection.execute(text(
                    "CREATE TABLE IF NOT EXISTS lifeplus_memories ("
                    "user_id VARCHAR(255) NOT NULL, memory_key VARCHAR(255) NOT NULL, value TEXT NOT NULL, "
                    "updated_at VARCHAR(64) NOT NULL, PRIMARY KEY(user_id, memory_key))"
                ))
                await connection.execute(text(
                    "CREATE TABLE IF NOT EXISTS lifeplus_session_contexts ("
                    "user_id VARCHAR(255) NOT NULL, session_id VARCHAR(255) NOT NULL, "
                    "memory_brief TEXT NOT NULL, model_history_json LONGTEXT NOT NULL, "
                    "updated_at VARCHAR(64) NOT NULL, PRIMARY KEY(user_id, session_id))"
                ))

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
        if self.mysql:
            await self.mysql.dispose()

    async def list(self, user_id: str) -> list[dict]:
        if self.mysql:
            from sqlalchemy import text

            async with self.mysql.connect() as connection:
                rows = (await connection.execute(text(
                    "SELECT memory_key AS `key`,value,updated_at FROM lifeplus_memories "
                    "WHERE user_id=:user_id ORDER BY updated_at DESC"
                ), {"user_id": user_id})).mappings().all()
                return [dict(row) for row in rows]
        return await self.db.fetchall(
            "SELECT key, value, updated_at FROM memories WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        )

    async def get_map(self, user_id: str) -> dict[str, str]:
        rows = await self.list(user_id)
        temporary = {"current_location", "当前位置", "current_city", "当前城市", "last_selected_poi"}
        return {row["key"]: row["value"] for row in rows
                if row["key"] not in temporary or recent(row.get("updated_at"), 300)}

    async def put(self, user_id: str, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if self.mysql:
            from sqlalchemy import text

            async with self.mysql.begin() as connection:
                await connection.execute(text(
                    "INSERT INTO lifeplus_memories(user_id,memory_key,value,updated_at) "
                    "VALUES(:user_id,:key,:value,:updated_at) ON DUPLICATE KEY UPDATE "
                    "value=VALUES(value),updated_at=VALUES(updated_at)"
                ), {"user_id": user_id, "key": key, "value": value, "updated_at": now})
            return
        await self.db.execute(
            "INSERT INTO memories(user_id,key,value,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (user_id, key, value, now),
        )

    async def delete(self, user_id: str, key: str) -> None:
        if self.mysql:
            from sqlalchemy import text

            async with self.mysql.begin() as connection:
                await connection.execute(text(
                    "DELETE FROM lifeplus_memories WHERE user_id=:user_id AND memory_key=:key"
                ), {"user_id": user_id, "key": key})
            return
        await self.db.execute("DELETE FROM memories WHERE user_id=? AND key=?", (user_id, key))

    async def build_context(self, user_id: str, session_id: str, messages: list[Any],
                            task_state: dict[str, Any] | None = None) -> dict[str, Any]:
        cached = await self._load_cached_context(user_id, session_id)
        normalized = [item for item in (_message_dict(message) for message in messages) if item]
        if normalized:
            history = self._trim_history(normalized)
            brief = self._brief(normalized, task_state or {})
        else:
            history = self._trim_history(cached.get("model_history", []))
            brief = cached.get("memory_brief") or self._brief(history, task_state or {})
        available = await self.get_map(user_id)
        preferences = {}
        used = 0
        for key, value in available.items():
            size = len(key) + len(value)
            if len(preferences) >= 20:
                break
            if used + size <= 4000:
                preferences[key] = value
                used += size
        from .conversation import load_references
        references = await load_references(self.db, user_id, session_id)
        return {"preferences": preferences, "memory_brief": brief, "model_history": history,
                "conversation_references": references}

    async def save_context(self, user_id: str, session_id: str, messages: list[Any],
                           task_state: dict[str, Any] | None = None) -> None:
        normalized = [item for item in (_message_dict(message) for message in messages) if item]
        history = self._trim_history(normalized)
        now = datetime.now(timezone.utc).isoformat()
        payload = {"memory_brief": self._brief(normalized, task_state or {}), "model_history": history,
                   "updated_at": now}
        await self._save_durable_context(user_id, session_id, payload, now)
        if self.redis:
            await self.redis.setex(self._cache_key(user_id, session_id), max(1, int(self.context_seconds)),
                                   json.dumps(payload, ensure_ascii=False))

    def _trim_history(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        selected: list[dict[str, str]] = []
        used = 0
        for message in reversed(messages[-self.history_messages:]):
            content = message.get("content", "")[-2000:]
            remaining = self.history_characters - used
            if remaining <= 0:
                break
            selected.append({"role": message.get("role", "user"), "content": content[-remaining:]})
            used += min(len(content), remaining)
        return list(reversed(selected))

    @staticmethod
    def _brief(history: list[dict[str, str]], task_state: dict[str, Any]) -> str:
        recent = "；".join(f"{item['role']}:{item['content'][:160]}" for item in history[-6:])
        older = "；".join(f"{item['role']}:{item['content'][:120]}" for item in history[-12:-6])
        pending = task_state.get("pending_field") or (task_state.get("interrupt") or {}).get("field")
        constraints = task_state.get("slots") or {}
        parts = [f"较早对话（摘录）：{older}" if older else "", f"近期对话：{recent}" if recent else ""]
        if pending:
            parts.append(f"待补充：{pending}")
        if constraints:
            parts.append("当前约束：" + json.dumps(constraints, ensure_ascii=False, sort_keys=True)[:800])
        return "\n".join(part for part in parts if part)[:2000]

    async def _load_cached_context(self, user_id: str, session_id: str) -> dict[str, Any]:
        if self.redis:
            value = await self.redis.get(self._cache_key(user_id, session_id))
            if value:
                try:
                    payload = json.loads(value)
                except (TypeError, ValueError):
                    payload = {}
                if isinstance(payload, dict) and recent(payload.get("updated_at"), self.context_seconds):
                    return payload
                await self.redis.delete(self._cache_key(user_id, session_id))
        payload = await self._load_durable_context(user_id, session_id)
        if not recent(payload.get("updated_at"), self.context_seconds):
            return {}
        if payload and self.redis:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["updated_at"])).total_seconds()
            await self.redis.setex(self._cache_key(user_id, session_id), max(1, int(self.context_seconds - age)),
                                   json.dumps(payload, ensure_ascii=False))
        return payload

    async def _load_durable_context(self, user_id: str, session_id: str) -> dict[str, Any]:
        if self.mysql:
            from sqlalchemy import text

            async with self.mysql.connect() as connection:
                row = (await connection.execute(text(
                    "SELECT memory_brief,model_history_json,updated_at FROM lifeplus_session_contexts "
                    "WHERE user_id=:user_id AND session_id=:session_id"
                ), {"user_id": user_id, "session_id": session_id})).mappings().first()
                if not row:
                    return {}
                return {"memory_brief": row["memory_brief"],
                        "model_history": json.loads(row["model_history_json"] or "[]"), "updated_at": row["updated_at"]}
        row = await self.db.fetchone(
            "SELECT memory_brief,model_history_json,updated_at FROM session_contexts WHERE user_id=? AND session_id=?",
            (user_id, session_id),
        )
        return {"memory_brief": row["memory_brief"],
                "model_history": json.loads(row["model_history_json"] or "[]"), "updated_at": row["updated_at"]} if row else {}

    async def _save_durable_context(self, user_id: str, session_id: str, payload: dict[str, Any], now: str) -> None:
        history_json = json.dumps(payload["model_history"], ensure_ascii=False)
        if self.mysql:
            from sqlalchemy import text

            async with self.mysql.begin() as connection:
                await connection.execute(text(
                    "INSERT INTO lifeplus_session_contexts(user_id,session_id,memory_brief,model_history_json,updated_at) "
                    "VALUES(:user_id,:session_id,:brief,:history,:updated_at) ON DUPLICATE KEY UPDATE "
                    "memory_brief=VALUES(memory_brief),model_history_json=VALUES(model_history_json),"
                    "updated_at=VALUES(updated_at)"
                ), {"user_id": user_id, "session_id": session_id, "brief": payload["memory_brief"],
                    "history": history_json, "updated_at": now})
            return
        await self.db.execute(
            "INSERT INTO session_contexts(user_id,session_id,memory_brief,model_history_json,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET "
            "memory_brief=excluded.memory_brief,model_history_json=excluded.model_history_json,updated_at=excluded.updated_at",
            (user_id, session_id, payload["memory_brief"], history_json, now),
        )

    @staticmethod
    def _cache_key(user_id: str, session_id: str) -> str:
        return f"lifeplus:session:{user_id}:{session_id}"
