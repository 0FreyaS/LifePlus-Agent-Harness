from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .persistence import Database

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """Persist due notifications atomically so restart and overlapping ticks do not duplicate delivery."""

    def __init__(self, db: Database, interval: float = 1):
        self.db = db
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.tick()
        self._task = asyncio.create_task(self._run(), name="lifeplus-reminders")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def tick(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        assert self.db.conn
        async with self.db.lock:
            await self.db.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = await (await self.db.conn.execute(
                    "SELECT id,user_id,title FROM reminders WHERE status='scheduled' "
                    "AND julianday(remind_at)<=julianday(?)", (now,))).fetchall()
                for row in rows:
                    await self.db.conn.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,NULL)",
                                               (row["id"], row["user_id"], row["id"], row["title"], now))
                    await self.db.conn.execute("UPDATE reminders SET status='delivered' WHERE id=?", (row["id"],))
                await self.db.conn.commit()
                return len(rows)
            except BaseException:
                await self.db.conn.rollback()
                raise

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.tick()
            except Exception:
                logger.exception("Reminder delivery tick failed; will retry")
