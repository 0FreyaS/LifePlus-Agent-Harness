import aiosqlite
import asyncio


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, user_id TEXT NOT NULL,
  query TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, answer TEXT,
  interrupt_json TEXT, last_seq INTEGER NOT NULL DEFAULT 0,
  domain TEXT NOT NULL DEFAULT '',
  parent_task_id TEXT,
  thread_id TEXT NOT NULL DEFAULT '',
  plan_json TEXT,
  request_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS tasks_session_idx ON tasks(session_id, created_at DESC);
CREATE TABLE IF NOT EXISTS runtime_usage (
  task_id TEXT PRIMARY KEY, active_seconds REAL NOT NULL DEFAULT 0,
  model_calls INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  resumes INTEGER NOT NULL DEFAULT 0, refreshes INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS input_event_decisions (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  request_id TEXT NOT NULL, status TEXT NOT NULL, decision_json TEXT NOT NULL,
  created_at TEXT NOT NULL, error_type TEXT
);
CREATE INDEX IF NOT EXISTS input_event_decisions_session_idx
  ON input_event_decisions(user_id,session_id,created_at);
CREATE TABLE IF NOT EXISTS input_event_spans (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, input_event_id TEXT NOT NULL,
  type TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS input_event_spans_input_idx ON input_event_spans(input_event_id,seq);
CREATE TABLE IF NOT EXISTS events (
  task_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL, created_at TEXT NOT NULL, data_json TEXT NOT NULL,
  PRIMARY KEY(task_id, seq)
);
CREATE TABLE IF NOT EXISTS task_reviews (
  task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  is_bad_case INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT '',
  root_cause TEXT NOT NULL DEFAULT '', expected_answer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_reviews_user_idx ON task_reviews(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS operations (
  idempotency_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  tool_name TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
  user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS session_contexts (
  user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  memory_brief TEXT NOT NULL DEFAULT '', model_history_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL, PRIMARY KEY(user_id, session_id)
);
CREATE TABLE IF NOT EXISTS conversation_references (
  task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  domain TEXT NOT NULL, payload_json TEXT NOT NULL, expires_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversation_references_session_idx
  ON conversation_references(user_id,session_id,updated_at DESC);
CREATE TABLE IF NOT EXISTS conversation_inputs (
  request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
  message TEXT NOT NULL, mode TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_jobs (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL, dependencies_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 2,
  timeout_seconds REAL NOT NULL DEFAULT 8, result_json TEXT, error TEXT,
  expires_at TEXT,
  deadline_at TEXT,
  available_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tool_jobs_status_idx ON tool_jobs(status, available_at);
CREATE INDEX IF NOT EXISTS tool_jobs_result_idx ON tool_jobs(task_id, tool_name, status, created_at);
CREATE TABLE IF NOT EXISTS tool_dead_letters (
  job_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL, error TEXT NOT NULL, attempts INTEGER NOT NULL,
  failed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS itinerary_items (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
  remind_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_receipts (
  tool_name TEXT NOT NULL, user_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(tool_name,user_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, reminder_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        columns = await (await self.conn.execute("PRAGMA table_info(tasks)")).fetchall()
        existing_columns = {column[1] for column in columns}
        for name, definition in {
            "domain": "TEXT NOT NULL DEFAULT ''", "parent_task_id": "TEXT",
            "thread_id": "TEXT NOT NULL DEFAULT ''", "plan_json": "TEXT",
        }.items():
            if name not in existing_columns:
                await self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        await self.conn.execute("CREATE INDEX IF NOT EXISTS tasks_parent_idx ON tasks(parent_task_id)")
        queue_columns = await (await self.conn.execute("PRAGMA table_info(tool_jobs)")).fetchall()
        if "expires_at" not in {column[1] for column in queue_columns}:
            await self.conn.execute("ALTER TABLE tool_jobs ADD COLUMN expires_at TEXT")
        if "deadline_at" not in {column[1] for column in queue_columns}:
            await self.conn.execute("ALTER TABLE tool_jobs ADD COLUMN deadline_at TEXT")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        assert self.conn
        async with self.lock:
            try:
                cursor = await self.conn.execute(sql, params)
                await self.conn.commit()
                return cursor.rowcount
            except BaseException:
                await self.conn.rollback()
                raise

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        assert self.conn
        async with self.lock:
            cursor = await self.conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        assert self.conn
        async with self.lock:
            cursor = await self.conn.execute(sql, params)
            return [dict(row) for row in await cursor.fetchall()]

    async def claim_tool_job(self, now: str) -> dict | None:
        """Atomically claim one ready queue job for a worker."""
        assert self.conn
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.conn.execute(
                    "SELECT * FROM tool_jobs WHERE status='queued' AND available_at<=? "
                    "ORDER BY created_at LIMIT 1",
                    (now,),
                )
                row = await cursor.fetchone()
                if not row:
                    await self.conn.commit()
                    return None
                await self.conn.execute(
                    "UPDATE tool_jobs SET status='running',attempt=attempt+1,updated_at=? "
                    "WHERE id=? AND status='queued'",
                    (now, row["id"]),
                )
                await self.conn.commit()
                return dict(row)
            except BaseException:
                await self.conn.rollback()
                raise

    async def publish(self, task_id: str, event_type: str, data: dict) -> dict:
        import json
        import uuid
        from datetime import datetime, timezone

        assert self.conn
        now = datetime.now(timezone.utc).isoformat()
        event_id = str(uuid.uuid4())
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self.conn.execute(
                    "SELECT last_seq,parent_task_id FROM tasks WHERE id = ?", (task_id,)
                )
                row = await cursor.fetchone()
                if not row:
                    raise KeyError(f"Unknown task: {task_id}")
                seq = row[0] + 1
                await self.conn.execute(
                    "UPDATE tasks SET last_seq=?, updated_at=? WHERE id=?",
                    (seq, now, task_id),
                )
                await self.conn.execute(
                    "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, seq, event_id, event_type, now, json.dumps(data, ensure_ascii=False)),
                )
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        event = {"event_id": event_id, "task_id": task_id, "seq": seq,
                 "type": event_type, "created_at": now, **data}
        if row["parent_task_id"]:
            forwarded_type = "subtask." + event_type if event_type.startswith(("task.", "answer.completed", "interrupt.")) else event_type
            await self.publish(row["parent_task_id"], forwarded_type,
                               {**data, "child_task_id": task_id, "child_seq": seq})
        return event
