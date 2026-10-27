import asyncio
import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ..adapters.demo import DemoServices
from ..adapters.mcp_client import LocalMCPClient
from ..config import get_settings
from ..model_roles import build_models
from ..access import ShowcaseAccess, validate_showcase
from ..conversation import save_reference, selection_reply, city_reply
from ..harness import build_graph, CITY_NAMES
from ..memory import MemoryRepository
from ..models import ChatRequest, LocalLifeRequest, ResumeRequest, RefreshRequest, RouteReplanRequest
from ..freshness import FreshnessPolicy
from ..interactions import cancel_running, effective_values, refresh_waiting, resume_waiting
from ..runtime_limits import RuntimeLimits, execution_scope
from ..diagnostics import summarize
from ..persistence import Database
from ..rag import KnowledgeRetriever
from ..reminders import ReminderScheduler
from ..runtime_context import RunContext
from ..skill_registry import LifeSkillRegistry
from ..tool_queue import ToolQueue
from ..tools import LifeTools
from ..orchestration import TaskCoordinator, TERMINAL, session_messages, task_view
from ..retry import can_retry, retry_failed
from ..route_presentation import MapPreviewError, RouteMapRenderer, route_record, route_rows, stored_route

settings = get_settings()
root = Path(settings.lifeplus_root).resolve() if settings.lifeplus_root else Path(__file__).resolve().parents[3]
frontend_dist = root / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_showcase(settings)
    freshness = FreshnessPolicy.from_settings(settings)
    limits = RuntimeLimits.from_settings(settings)
    db = Database(str(settings.database_file))
    await db.open()
    await db.execute("UPDATE tasks SET status='interrupted', updated_at=? WHERE status='running'",
                     (datetime.now(timezone.utc).isoformat(),))
    mcp_env = dict(__import__("os").environ)
    if settings.amap_maps_api_key:
        mcp_env["AMAP_MAPS_API_KEY"] = settings.amap_maps_api_key
    mcp_env["LIFEPLUS_DATA_PATH"] = str(settings.database_file.resolve())
    mcp = LocalMCPClient(mcp_env)
    demo = DemoServices()
    llm, model_roles = build_models(settings)
    if settings.checkpoint_postgres_dsn:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver_context = AsyncPostgresSaver.from_conn_string(settings.checkpoint_postgres_dsn)
    else:
        saver_context = AsyncSqliteSaver.from_conn_string(str(settings.database_file))
    saver = await saver_context.__aenter__()
    await saver.setup()
    memory = MemoryRepository(
        db, settings.redis_url, settings.mysql_dsn,
        settings.memory_history_messages, settings.memory_history_characters,
        freshness.context_seconds,
    )
    await memory.open()
    skill_registry = LifeSkillRegistry(str(root / "skills"))
    retriever = KnowledgeRetriever(settings.chroma_directory, root / "knowledge")
    retriever.open()

    async def dispatch_tool(tool_name: str, arguments: dict, task_id: str) -> dict:
        task = await db.fetchone("SELECT mode FROM tasks WHERE id=?", (task_id,))
        if task and task["mode"] == "demo":
            if tool_name == "save_itinerary":
                return {"status": "saved", "item_id": arguments.get("idempotency_key"),
                        "title": arguments.get("title"), "simulated": True}
            if tool_name == "create_reminder":
                return {"status": "scheduled", "reminder_id": arguments.get("idempotency_key"),
                        "title": arguments.get("title"), "remind_at": arguments.get("remind_at"),
                        "simulated": True}
            return demo.query(tool_name, arguments)
        if not mcp.session:
            await mcp.open()
        return await mcp.call(tool_name, arguments)

    tool_queue = ToolQueue(
        db, dispatch_tool, db.publish, settings.tool_queue_workers,
        settings.tool_queue_timeout_seconds, settings.tool_queue_max_retries,
        freshness=freshness,
    )
    await tool_queue.start()
    reminder_scheduler = ReminderScheduler(db)
    await reminder_scheduler.start()
    app.state.reminder_scheduler = reminder_scheduler
    app.state.db = db
    app.state.route_maps = RouteMapRenderer(settings.amap_maps_api_key)
    app.state.mcp = mcp
    app.state.saver_context = saver_context
    app.state.graph = build_graph(saver)
    app.state.context = RunContext(
        settings.demo_user_id, db, memory, demo, mcp,
        tool_queue, skill_registry, retriever, llm, str(root / "skills"), freshness, limits,
        model_roles=model_roles, planner_variant=settings.planner_variant,
    )
    app.state.locks = {}
    app.state.runners = {}
    app.state.closing = False
    app.state.coordinator = TaskCoordinator(app, run_task)
    yield
    app.state.closing = True
    for runner in list(app.state.runners.values()):
        runner.cancel()
    await asyncio.gather(*list(app.state.runners.values()), return_exceptions=True)
    await tool_queue.close()
    await reminder_scheduler.close()
    await memory.close()
    await mcp.close()
    await saver_context.__aexit__(None, None, None)
    await db.close()


app = FastAPI(title="LifePlus Agent Harness", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.origins, allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=False)
app.add_middleware(ShowcaseAccess, settings=settings)


def user_id(request: Request, demo_header: str | None = None) -> str:
    if settings.app_mode == "showcase":
        owner = getattr(request.state, "showcase_owner", None)
        if not owner:
            raise HTTPException(401, "展示身份未验证")
        return owner
    if settings.app_mode in {"local", "demo"}:
        return demo_header or settings.demo_user_id
    raise HTTPException(503, "生产身份认证尚未接入，已拒绝使用演示身份或未验证 Bearer 令牌")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


async def ensure_session(request: Request, session_id: str, owner: str) -> dict:
    row = await request.app.state.db.fetchone("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, owner))
    if not row:
        raise HTTPException(404, "会话不存在")
    return row


@app.get("/api/health")
async def health(request: Request):
    return {
        "status": "ok",
        "auth_mode": settings.app_mode,
        "default_task_mode": "live",
        "model_configured": bool(settings.model_base_url and settings.model_api_key and settings.model_name),
        "model_name": settings.model_name if settings.model_api_key else None,
        "maps_configured": bool(settings.amap_maps_api_key),
        "checkpoint_backend": "postgresql" if settings.checkpoint_postgres_dsn else "sqlite",
        "session_cache": request.app.state.context.memory.cache_backend,
        "long_term_memory": request.app.state.context.memory.durable_backend,
        "tool_queue_workers": settings.tool_queue_workers,
        "rag_backend": request.app.state.context.retriever.backend,
        "reminder_delivery": "in_app",
        "tools_backend": "real_mcp",
        "response_streaming": True,
        "multi_task": True,
    }


@app.get("/api/tools")
async def tools_catalog():
    return LifeTools(str(settings.database_file), settings.amap_maps_api_key).catalog()


@app.get("/api/notifications")
async def notifications(request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    return await request.app.state.db.fetchall(
        "SELECT id,reminder_id,title,created_at,read_at FROM notifications "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (owner,))


@app.post("/api/notifications/{notification_id}/read")
async def read_notification(notification_id: str, request: Request,
                            x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    db = request.app.state.db
    row = await db.fetchone("SELECT id FROM notifications WHERE id=? AND user_id=?", (notification_id, owner))
    if not row:
        raise HTTPException(404, "通知不存在")
    await db.execute("UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND user_id=?",
                     (datetime.now(timezone.utc).isoformat(), notification_id, owner))
    return {"id": notification_id, "read": True}


@app.post("/api/reminders/{reminder_id}/cancel")
async def cancel_reminder(reminder_id: str, request: Request,
                          x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    result = await request.app.state.context.mcp.call(
        "cancel_reminder", {"user_id": owner, "reminder_id": reminder_id})
    if result.get("error"):
        raise HTTPException(409 if result.get("error_code") == "ALREADY_DELIVERED" else 404, result["error"])
    return result


@app.post("/api/sessions")
async def create_session(request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await request.app.state.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                                       (session_id, owner, "新对话", now, now))
    return {"id": session_id, "title": "新对话", "created_at": now}


@app.get("/api/sessions")
async def list_sessions(request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    return await request.app.state.db.fetchall(
        "SELECT id,title,created_at,updated_at FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 100", (owner,))


@app.get("/api/sessions/{session_id}/messages")
async def messages(session_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    return await session_messages(request.app.state.db, session_id)


@app.post("/api/sessions/{session_id}/messages")
async def submit(session_id: str, payload: ChatRequest, request: Request,
                 x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    db = request.app.state.db
    request_id = payload.request_id or str(uuid.uuid4())
    payload = payload.model_copy(update={"request_id": request_id})
    async with request.app.state.locks.setdefault("conversation-input:" + owner + ":" + session_id, asyncio.Lock()):
        previous = await db.fetchone("SELECT * FROM conversation_inputs WHERE request_id=?", (request_id,))
        if previous:
            if any(previous[key] != value for key, value in {"user_id": owner, "session_id": session_id,
                                                           "message": payload.message, "mode": payload.mode}.items()):
                raise HTTPException(409, "请求标识已用于其他消息")
            return {**json.loads(previous["response_json"]), "duplicate": True}
        existing = await db.fetchone("SELECT id FROM tasks WHERE request_id=?", (request_id,))
        if existing:
            return await enqueue_message(session_id, payload, request, owner)
        waiting = await db.fetchall("SELECT * FROM tasks WHERE session_id=? AND user_id=? AND status='waiting_user' AND domain!='multi'",
                                    (session_id, owner))
        matched = selection_reply(payload.message, waiting)
        city_task = city_reply(payload.message, waiting, CITY_NAMES)
        if matched and city_task and matched[0]["id"] != city_task["id"]:
            raise HTTPException(409, "这条回复可能属于不同任务，请使用对应任务卡片补充或选择。")
        if matched or city_task:
            task = matched[0] if matched else city_task
            value = {"index": matched[1]} if matched else {"text": payload.message}
            pending = json.loads(task["interrupt_json"])
            response, _ = await resume_waiting(request.app, task["id"], ResumeRequest(
                interrupt_id=pending["id"], action="answer", value=value), owner, run_task)
            response.update(task_id=task.get("parent_task_id") or task["id"], resumed_task_id=task["id"],
                            session_id=session_id, resumed=True)
            await db.execute("INSERT INTO conversation_inputs VALUES(?,?,?,?,?,?,?)",
                             (request_id, owner, session_id, payload.message, payload.mode,
                              json.dumps(response), datetime.now(timezone.utc).isoformat()))
            return response
        if re.fullmatch(r"(?:我选|选择|选)?第?([一二三四五六七八九十]|\d+)(?:个结果|个|项|家|条)?[吧。！!]*", payload.message):
            raise HTTPException(409, "当前候选不明确、已过期或多个任务同时等待，请在对应任务卡片中选择。")
        return await enqueue_message(session_id, payload, request, owner)


@app.post("/api/local-life/tasks")
async def submit_local_life(payload: LocalLifeRequest, request: Request,
                           x_demo_user: str | None = Header(default=None)):
    from pydantic import ValidationError
    from ..tools.service import LocalSearchArgs, LocalRouteArgs, PlaceDetailArgs

    schema = {"local_search": LocalSearchArgs, "local_detail": PlaceDetailArgs, "local_route": LocalRouteArgs}[payload.action]
    try:
        parameters = schema.model_validate(payload.parameters).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise HTTPException(422, "本地生活参数不合法：" + ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())) from exc
    labels = {"local_search": "本地生活检索", "local_detail": "查看商家详情", "local_route": "商家路线规划"}
    query = labels[payload.action] + "：" + json.dumps(parameters, ensure_ascii=False, sort_keys=True)
    prepared = {"domain": "local_life", "action": payload.action, "slots": parameters,
                "input_request": {"action": payload.action, "parameters": parameters}}
    return await enqueue_message(payload.session_id, ChatRequest(message=query, mode=payload.mode, request_id=payload.request_id),
                                 request, user_id(request, x_demo_user), prepared)


async def enqueue_message(session_id: str, payload: ChatRequest, request: Request, owner: str,
                          prepared: dict | None = None):
    await ensure_session(request, session_id, owner)
    db = request.app.state.db
    request_id = payload.request_id or str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    mode = payload.mode
    async with request.app.state.locks.setdefault("admission:user:" + owner, asyncio.Lock()):
        if await db.fetchone("SELECT request_id FROM conversation_inputs WHERE request_id=?", (request_id,)):
            raise HTTPException(409, "请求标识已用于会话补充，不能用于创建其他任务")
        previous = await db.fetchone("SELECT id,user_id,session_id,status,query,mode,plan_json FROM tasks WHERE request_id=?", (request_id,))
        if previous:
            if previous["user_id"] != owner or previous["session_id"] != session_id:
                raise HTTPException(409, "请求标识已被使用")
            if previous["query"] != payload.message or previous["mode"] != mode:
                raise HTTPException(409, "同一请求标识不能用于不同内容或运行模式")
            if json.loads(previous.get("plan_json") or "{}").get("input_request") != (prepared or {}).get("input_request"):
                raise HTTPException(409, "同一请求标识不能用于不同的结构化任务")
            return {"task_id": previous["id"], "status": previous["status"], "session_id": session_id, "duplicate": True}
        if mode == "live" and not request.app.state.context.llm:
            raise HTTPException(503, "真实接入模式需要配置 MODEL_BASE_URL、MODEL_API_KEY 和 MODEL_NAME")
        active = await db.fetchone(
            "SELECT COUNT(*) AS count FROM tasks WHERE user_id=? AND parent_task_id IS NULL AND status IN ('queued','running')",
            (owner,),
        )
        if active["count"] >= settings.max_active_tasks_per_user:
            raise HTTPException(429, {"code": "TOO_MANY_ACTIVE_TASKS", "message": "当前执行中的任务较多，请稍后再试。"}, headers={"Retry-After": "2"})
        await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at,request_id,thread_id,plan_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (task_id, session_id, owner, payload.message, "queued", mode, now, now, request_id, session_id + ":" + task_id,
                          json.dumps(prepared, ensure_ascii=False) if prepared else None))
    await db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    await db.publish(task_id, "task.queued", {"query": payload.message, "mode": mode})
    coordinator = request.app.state.coordinator
    coordinator.spawn(task_id, coordinator.start(task_id, session_id, owner, payload.message, mode))
    return {"task_id": task_id, "session_id": session_id, "status": "queued"}


async def run_task(app: FastAPI, task_id: str, session_id: str, owner: str, query: str, mode: str,
                   resume: dict | None = None, replay_config: dict | None = None,
                   continue_from_checkpoint: bool = False):
    db = app.state.db
    lock = app.state.locks.setdefault("execution:" + task_id, asyncio.Lock())
    async with lock:
        task_row = await db.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task_row or task_row["status"] == "cancelled":
            return
        thread_id = task_row.get("thread_id") or session_id
        now = datetime.now(timezone.utc).isoformat()
        if not await db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status!='cancelled'", (now, task_id)):
            return
        await db.publish(task_id, "task.started", {"mode": mode})
        config = replay_config or {"configurable": {"thread_id": thread_id}}
        config = {**config, "recursion_limit": app.state.context.limits.graph_steps}
        graph = app.state.graph
        base_context = app.state.context
        context = RunContext(
            owner, base_context.db, base_context.memory, base_context.demo,
            base_context.mcp, base_context.tool_queue,
            base_context.skill_registry, base_context.retriever,
            base_context.llm, base_context.skills_dir,
            base_context.freshness,
            base_context.limits,
            model_roles=base_context.model_roles, planner_variant=base_context.planner_variant,
        )
        if replay_config or continue_from_checkpoint:
            graph_input = None
        elif resume:
            graph_input = Command(resume=resume)
        else:
            from langchain_core.messages import HumanMessage
            history = await session_messages(db, session_id, completed_only=True,
                                             max_age_seconds=context.freshness.context_seconds)
            spec = json.loads(task_row.get("plan_json") or "{}")
            prepared = spec.get("prepared_decision") or ({key: spec[key] for key in ("domain", "action", "slots")} if spec.get("domain") else {})
            graph_input = {"messages": [*history, HumanMessage(content=query)], "user_id": owner,
                           "session_id": session_id, "task_id": task_id, "request_id": str(uuid.uuid4()),
                           "mode": mode, "original_query": query, "resolved_query": query,
                           "status": "running", "memory": {}, "slots": {}, "tool_result": {}, "operation_result": {},
                           "tool_args": {}, "tool_name": "", "operation_key": "", "answer": "", "error": "",
                           "memory_brief": "", "model_history": [], "skill_names": [],
                           "handoff_summary": {}, "rag_context": [], "prepared_decision": prepared}
        try:
            async with execution_scope(db, task_id, context.limits, "graph"):
                async for chunk in graph.astream(
                    graph_input, config=config, context=context, stream_mode="custom", subgraphs=True
                ):
                    namespace: tuple[str, ...] = ()
                    item = chunk
                    if isinstance(chunk, tuple) and len(chunk) == 2:
                        namespace, item = chunk
                    if isinstance(item, dict) and item.get("type"):
                        kind = item.pop("type")
                        if namespace:
                            item["subgraph_path"] = list(namespace)
                        await db.publish(task_id, kind, item)
            snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}}, subgraphs=True)
            pending = [intr for task in snapshot.tasks for intr in task.interrupts]
            if pending:
                intr = pending[-1]
                value = intr.value if isinstance(intr.value, dict) else {"question": str(intr.value)}
                interrupt_data = context.freshness.interrupt(intr.id, value, effective_values(snapshot))
                now = datetime.now(timezone.utc).isoformat()
                if not await db.execute("UPDATE tasks SET status='waiting_user',interrupt_json=?,updated_at=? WHERE id=? AND status!='cancelled'",
                                        (json.dumps(interrupt_data, ensure_ascii=False), now, task_id)):
                    return
                await db.publish(task_id, "interrupt.required", interrupt_data)
                return
            values = snapshot.values
            status = values.get("status", "completed")
            answer = values.get("answer", "任务结束。")
            now = datetime.now(timezone.utc).isoformat()
            await save_reference(db, values, context.freshness)
            if not await db.execute("UPDATE tasks SET status=?,answer=?,domain=?,interrupt_json=NULL,updated_at=? WHERE id=? AND status!='cancelled'",
                                    (status, answer, values.get("domain", "chat"), now, task_id)):
                return
            await db.publish(task_id, f"task.{status}", {"answer": answer})
            if not task_row.get("parent_task_id"):
                history = await session_messages(db, session_id, completed_only=True,
                                                 max_age_seconds=context.freshness.context_seconds)
                await context.memory.save_context(owner, session_id, history, values)
            await db.execute("UPDATE sessions SET title=CASE WHEN title='新对话' THEN ? ELSE title END,updated_at=? WHERE id=?",
                             (query[:30], now, session_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await context.tool_queue.cancel_task(task_id)
            now = datetime.now(timezone.utc).isoformat()
            error = f"{type(exc).__name__}: {str(exc)[:400]}"
            answer = f"这次没有完成：{str(exc)[:400]}"
            if not await db.execute("UPDATE tasks SET status='failed',answer=?,updated_at=? WHERE id=? AND status!='cancelled'",
                                    (answer, now, task_id)):
                return
            await db.publish(task_id, "task.failed", {"error": error, "answer": answer})
        finally:
            if task_row.get("parent_task_id"):
                await app.state.coordinator.refresh(task_row["parent_task_id"])


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, payload: ResumeRequest, request: Request,
                      x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    response, new_query = await resume_waiting(request.app, task_id, payload, owner, run_task)
    if new_query:
        task = await request.app.state.db.fetchone("SELECT session_id,mode FROM tasks WHERE id=?", (task_id,))
        created = await submit(task["session_id"], ChatRequest(message=new_query, mode=task["mode"]), request, x_demo_user)
        response["created_task_id"] = created["task_id"]
    return response


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    return await cancel_running(request.app, task_id, user_id(request, x_demo_user))


@app.post("/api/tasks/{task_id}/refresh")
async def refresh_task(task_id: str, payload: RefreshRequest, request: Request,
                       x_demo_user: str | None = Header(default=None)):
    return await refresh_waiting(request.app, task_id, payload.interrupt_id,
                                 user_id(request, x_demo_user), run_task)


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    return await retry_failed(request.app, task_id, user_id(request, x_demo_user))


@app.get("/api/tasks")
async def list_tasks(request: Request, session_id: str | None = None,
                     q: str | None = Query(None, max_length=200),
                     domain: str | None = None, status: str | None = None,
                     review_state: str | None = Query(None, pattern="^(all|unreviewed|reviewed|bad_case)$"),
                     limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                     x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    clauses = ["t.user_id=?", "t.parent_task_id IS NULL"]
    params: list = [owner]
    if session_id:
        await ensure_session(request, session_id, owner)
        clauses.append("t.session_id=?")
        params.append(session_id)
    if q:
        clauses.append("(t.query LIKE ? OR COALESCE(t.answer,'') LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if status:
        clauses.append("t.status=?")
        params.append(status)
    if domain:
        clauses.append("(t.domain=? OR t.id IN (SELECT task_id FROM events WHERE type='route.completed' AND json_extract(data_json,'$.domain')=?))")
        params.append(domain)
        params.append(domain)
    if review_state == "unreviewed":
        clauses.append("r.task_id IS NULL")
    elif review_state == "reviewed":
        clauses.append("r.task_id IS NOT NULL")
    elif review_state == "bad_case":
        clauses.append("r.is_bad_case=1")
    rows = await request.app.state.db.fetchall(
        "SELECT t.*, r.is_bad_case, r.category, r.root_cause, r.expected_answer, r.note, r.updated_at AS reviewed_at "
        "FROM tasks t LEFT JOIN task_reviews r ON r.task_id=t.id AND r.user_id=t.user_id WHERE "
        + " AND ".join(clauses) + " ORDER BY t.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    if rows:
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        route_events = await request.app.state.db.fetchall(
            f"SELECT task_id,data_json FROM events WHERE type='route.completed' AND task_id IN ({placeholders})",
            tuple(ids),
        )
        domains = {row["id"]: row.get("domain") or "chat" for row in rows}
        for event in route_events:
            payload = json.loads(event["data_json"])
            if not payload.get("child_task_id"):
                domains[event["task_id"]] = payload.get("domain", "chat")
    else:
        domains = {}
    result = []
    children_by_parent: dict[str, list] = {}
    if rows:
        children = await request.app.state.db.fetchall(
            f"SELECT * FROM tasks WHERE parent_task_id IN ({placeholders}) ORDER BY created_at,rowid", tuple(ids))
        for child in children:
            children_by_parent.setdefault(child["parent_task_id"], []).append(task_view(child))
    for row in rows:
        item = task_view(row)
        item["domain"] = domains.get(item["id"], "chat")
        if item["id"] in children_by_parent:
            item["children"] = children_by_parent[item["id"]]
        item["review"] = None if item["is_bad_case"] is None else {
            "is_bad_case": bool(item["is_bad_case"]), "category": item["category"],
            "root_cause": item["root_cause"], "expected_answer": item["expected_answer"],
            "note": item["note"], "updated_at": item["reviewed_at"],
        }
        result.append(item)
    return result


@app.get("/api/analytics/summary")
async def task_summary(request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    counts = await request.app.state.db.fetchone(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM tasks WHERE user_id=? AND parent_task_id IS NULL", (owner,))
    reviews = await request.app.state.db.fetchone(
        "SELECT COUNT(*) AS reviewed, SUM(is_bad_case) AS bad_cases FROM task_reviews WHERE user_id=?", (owner,))
    total = counts["total"] or 0
    reviewed = reviews["reviewed"] or 0
    return {"total": total, "failed": counts["failed"] or 0, "reviewed": reviewed,
            "unreviewed": max(0, total - reviewed), "bad_cases": reviews["bad_cases"] or 0}


@app.get("/api/tasks/{task_id}/review")
async def get_task_review(task_id: str, request: Request,
                          x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT id FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    review = await request.app.state.db.fetchone(
        "SELECT is_bad_case,category,root_cause,expected_answer,note,updated_at FROM task_reviews WHERE task_id=? AND user_id=?",
        (task_id, owner))
    if review:
        review["is_bad_case"] = bool(review["is_bad_case"])
    return review


@app.put("/api/tasks/{task_id}/review")
async def save_task_review(task_id: str, payload: dict, request: Request,
                           x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT id FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    fields = {key: str(payload.get(key, "")).strip() for key in ("category", "root_cause", "expected_answer", "note")}
    if any(len(value) > 4000 for value in fields.values()):
        raise HTTPException(422, "归因字段最多 4000 字")
    is_bad_case = bool(payload.get("is_bad_case", False))
    allowed = {"intent", "slot", "memory", "skill", "tool", "knowledge", "policy", "response_quality", "latency", "other", ""}
    if fields["category"] not in allowed:
        raise HTTPException(422, "无效的问题分类")
    now = datetime.now(timezone.utc).isoformat()
    await request.app.state.db.execute(
        "INSERT INTO task_reviews(task_id,user_id,is_bad_case,category,root_cause,expected_answer,note,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET user_id=excluded.user_id, "
        "is_bad_case=excluded.is_bad_case,category=excluded.category,root_cause=excluded.root_cause, "
        "expected_answer=excluded.expected_answer,note=excluded.note,updated_at=excluded.updated_at",
        (task_id, owner, int(is_bad_case), fields["category"], fields["root_cause"],
         fields["expected_answer"], fields["note"], now),
    )
    return {"is_bad_case": is_bad_case, **fields, "updated_at": now}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    result = task_view(task)
    children = await request.app.state.db.fetchall("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at,rowid", (task_id,))
    if children:
        result["children"] = [task_view(child) for child in children]
    result["can_retry"] = await can_retry(request.app.state.db, task, children, request.app.state.context.limits.retries)
    return result


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request, after_seq: int = Query(0, ge=0),
                      last_event_id: str | None = Header(default=None),
                      x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT id FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")

    async def stream():
        cursor = max(after_seq, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)
        while True:
            rows = await request.app.state.db.fetchall(
                "SELECT seq,event_id,type,created_at,data_json FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200",
                (task_id, cursor))
            for row in rows:
                cursor = row["seq"]
                payload = {"seq": cursor, "event_id": row["event_id"], "task_id": task_id,
                           "type": row["type"], "created_at": row["created_at"], **json.loads(row["data_json"])}
                yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            current = await request.app.state.db.fetchone("SELECT status FROM tasks WHERE id=?", (task_id,))
            if current and current["status"] in TERMINAL and not rows:
                break
            if await request.is_disconnected():
                break
            if not rows:
                yield ": heartbeat\n\n"
                await asyncio.sleep(0.08)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/sessions/{session_id}/routes")
async def session_routes(session_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner, db = user_id(request, x_demo_user), request.app.state.db
    if not await db.fetchone("SELECT id FROM sessions WHERE id=? AND user_id=?", (session_id, owner)):
        raise HTTPException(404, "会话不存在")
    routes, seen = [], set()
    for row in await route_rows(db, owner=owner, session_id=session_id):
        if row["task_id"] in seen:
            continue
        route = route_record(row, request.app.state.context.freshness.route_seconds)
        if route:
            seen.add(row["task_id"])
            routes.append({key: value for key, value in route.items() if key not in {"geometry", "steps"}})
    return list(reversed(routes))


@app.get("/api/tasks/{task_id}/route")
async def route_detail(task_id: str, request: Request, job_id: str | None = None,
                       option: int | None = Query(default=None, ge=0, le=4),
                       x_demo_user: str | None = Header(default=None)):
    return await stored_route(request.app.state.db, user_id(request, x_demo_user), task_id, job_id,
                              request.app.state.context.freshness.route_seconds, option)


@app.post("/api/tasks/{task_id}/route/replan", status_code=202)
async def replan_route(task_id: str, payload: RouteReplanRequest, request: Request,
                       x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    source = await stored_route(request.app.state.db, owner, task_id, payload.job_id,
                                request.app.state.context.freshness.route_seconds)
    # A UI mode change is an explicit structured request, not a new ambiguous text intent.
    slots = {"origin": source["origin"], "destination": source["destination"],
             "origin_name": source["origin_name"], "destination_name": source["destination_name"],
             "travel_mode": payload.travel_mode, "route_requested": True}
    mode_name = {"driving": "驾车", "walking": "步行", "bicycling": "骑行"}[payload.travel_mode]
    query = f"重新规划{mode_name}路线：{source['origin_name']} → {source['destination_name']}"
    prepared = {"domain": "navigation", "action": "navigation_route", "slots": slots,
                "input_request": {"action": "navigation_route", "source_task_id": task_id,
                                  "source_job_id": payload.job_id, "travel_mode": payload.travel_mode}}
    return await enqueue_message(source["session_id"], ChatRequest(message=query, mode="live", request_id=payload.request_id),
                                 request, owner, prepared)


@app.get("/api/tasks/{task_id}/route/map")
async def route_map(task_id: str, request: Request, job_id: str | None = None,
                    zoom: int = Query(default=0, ge=-4, le=4), large: bool = False,
                    option: int | None = Query(default=None, ge=0, le=4),
                    x_demo_user: str | None = Header(default=None)):
    route = await stored_route(request.app.state.db, user_id(request, x_demo_user), task_id, job_id,
                               request.app.state.context.freshness.route_seconds, option)
    try:
        image = await request.app.state.route_maps.render(route, zoom, large)
    except MapPreviewError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(image, media_type="image/png", headers={"Cache-Control": "private, max-age=120",
                                                           "Vary": "X-Demo-User, Authorization"})


@app.get("/api/tasks/{task_id}/diagnostics")
async def task_diagnostics(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    db = request.app.state.db
    root = await db.fetchone("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id(request, x_demo_user)))
    if not root:
        raise HTTPException(404, "任务不存在")
    children = await db.fetchall("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY created_at,rowid", (task_id,))
    tasks = [root, *children]
    identifiers = tuple(row["id"] for row in tasks)
    marks = ",".join("?" for _ in identifiers)
    rows = await db.fetchall(f"SELECT * FROM events WHERE task_id IN ({marks}) ORDER BY created_at,seq", identifiers)
    events = [{"task_id": row["task_id"], "seq": row["seq"], "type": row["type"],
               "created_at": row["created_at"], **json.loads(row["data_json"])} for row in rows]
    counters = await db.fetchall(f"SELECT * FROM runtime_usage WHERE task_id IN ({marks})", identifiers)
    return summarize(tasks, events, counters)


@app.get("/api/tasks/{task_id}/trace")
async def task_trace(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT id FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    rows = await request.app.state.db.fetchall("SELECT seq,event_id,type,created_at,data_json FROM events WHERE task_id=? ORDER BY seq", (task_id,))
    return [{"seq": r["seq"], "event_id": r["event_id"], "type": r["type"],
             "created_at": r["created_at"], **json.loads(r["data_json"])} for r in rows]


@app.get("/api/tasks/{task_id}/graph")
async def task_graph(task_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    task = await request.app.state.db.fetchone("SELECT id,status FROM tasks WHERE id=? AND user_id=?", (task_id, owner))
    if not task:
        raise HTTPException(404, "任务不存在")
    children = await request.app.state.db.fetchall("SELECT * FROM tasks WHERE parent_task_id=? ORDER BY rowid", (task_id,))
    if children:
        specs = {json.loads(child["plan_json"])["id"]: child for child in children}
        depended_on = {dependency for child in children for dependency in json.loads(child["plan_json"]).get("depends_on", [])}
        edges = []
        for child in children:
            dependencies = json.loads(child["plan_json"]).get("depends_on", [])
            edges.extend({"source": specs[dependency]["id"], "target": child["id"]} for dependency in dependencies)
            if not dependencies:
                edges.append({"source": "plan", "target": child["id"]})
            if json.loads(child["plan_json"])["id"] not in depended_on:
                edges.append({"source": child["id"], "target": "aggregate"})
        return {"nodes": [{"id": "plan", "label": "任务规划与分发", "status": "completed", "group": "harness"},
                          *[{"id": child["id"], "label": child["query"], "status": child["status"], "group": "subtask"} for child in children],
                          {"id": "aggregate", "label": "结果汇总", "status": task["status"], "group": "harness"}], "edges": edges}
    return {"nodes": [
        {"id": "route", "label": "意图路由", "group": "harness"},
        {"id": "memory", "label": "记忆召回", "group": "harness"},
        *[{"id": f"{domain}_agent", "label": f"{label} Agent", "group": "subgraph"} for domain, label in (("weather", "天气"), ("navigation", "地点与路线"), ("music", "音乐"), ("wellbeing", "健康知识"), ("planning", "行程提醒"), ("local_life", "本地生活"), ("chat", "通用"))],
        {"id": "respond", "label": "结果校验与回复", "group": "harness"},
    ], "edges": [{"source": "memory", "target": "route"},
        *[{"source": "route", "target": f"{domain}_agent", "label": domain} for domain in ("weather", "navigation", "music", "wellbeing", "planning", "local_life", "chat")],
        *[{"source": f"{domain}_agent", "target": "respond"} for domain in ("weather", "navigation", "music", "wellbeing", "planning", "local_life", "chat")]]}


@app.get("/api/sessions/{session_id}/checkpoints")
async def checkpoints(session_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    result = []
    rows = await request.app.state.db.fetchall("SELECT DISTINCT thread_id FROM tasks WHERE session_id=? ORDER BY created_at DESC LIMIT 20", (session_id,))
    threads = list(dict.fromkeys([row["thread_id"] or session_id for row in rows] + [session_id]))
    for thread in threads:
        async for snap in request.app.state.graph.aget_state_history({"configurable": {"thread_id": thread}}, limit=15):
            result.append({"checkpoint_id": snap.config["configurable"].get("checkpoint_id"),
                           "thread_id": thread, "next": list(snap.next), "metadata": snap.metadata,
                           "values": {key: snap.values.get(key) for key in ("domain", "action", "status", "answer", "tool_result", "original_query") if snap.values.get(key) is not None}})
        if len(result) >= 50:
            return result[:50]
    return result


@app.post("/api/sessions/{session_id}/checkpoints/{checkpoint_id}/replay")
async def replay_checkpoint(session_id: str, checkpoint_id: str, request: Request,
                            x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    active = await request.app.state.db.fetchone(
        "SELECT id FROM tasks WHERE session_id=? AND user_id=? AND status IN ('queued','running','waiting_user') LIMIT 1",
        (session_id, owner),
    )
    if active:
        raise HTTPException(409, "请先结束当前任务，再从历史检查点回放")
    points = await checkpoints(session_id, request, x_demo_user)
    source = next((point for point in points if point["checkpoint_id"] == checkpoint_id), None)
    if not source:
        raise HTTPException(404, "检查点不存在或已过期")
    checkpoint_config = {"configurable": {"thread_id": source["thread_id"], "checkpoint_id": checkpoint_id}}
    snapshot = await request.app.state.graph.aget_state(checkpoint_config)
    if not snapshot.values or not snapshot.next:
        raise HTTPException(409, "该检查点没有可继续执行的后续节点")

    task_id = str(uuid.uuid4())
    query = snapshot.values.get("original_query", "检查点回放")
    now = datetime.now(timezone.utc).isoformat()
    await request.app.state.db.execute(
        "INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at,request_id,thread_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (task_id, session_id, owner, query, "queued", "demo", now, now, str(uuid.uuid4()), source["thread_id"]),
    )
    source_config = {"configurable": {**snapshot.config["configurable"],
                                       "thread_id": source["thread_id"],
                                       "checkpoint_id": checkpoint_id,
                                       "checkpoint_ns": snapshot.config["configurable"].get("checkpoint_ns", "")}}
    branch_config = await request.app.state.graph.aupdate_state(
        source_config,
        {"task_id": task_id, "request_id": str(uuid.uuid4()), "status": "running",
         "mode": "demo", "answer": "", "error": "", "operation_result": {}},
    )
    await request.app.state.db.publish(task_id, "task.queued", {
        "query": query, "mode": "demo", "replay_from": checkpoint_id,
    })
    request.app.state.coordinator.spawn(task_id, run_task(request.app, task_id, session_id, owner, query, "demo",
                                                       replay_config=branch_config))
    return {"task_id": task_id, "session_id": session_id, "status": "queued", "replay_from": checkpoint_id}


@app.get("/api/sessions/{session_id}/memories")
async def memories(session_id: str, request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    return await request.app.state.context.memory.list(owner)


@app.put("/api/sessions/{session_id}/memories/{key}")
async def put_memory(session_id: str, key: str, payload: dict, request: Request,
                     x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    value = str(payload.get("value", "")).strip()
    if not key or not value or len(value) > 500:
        raise HTTPException(422, "记忆键值不能为空，内容最多 500 字")
    await request.app.state.context.memory.put(owner, key, value)
    return {"key": key, "value": value}


@app.delete("/api/sessions/{session_id}/memories/{key}")
async def delete_memory(session_id: str, key: str, request: Request,
                        x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    await ensure_session(request, session_id, owner)
    await request.app.state.context.memory.delete(owner, key)
    return {"deleted": key}


@app.get("/api/life/state")
async def life_state(request: Request, x_demo_user: str | None = Header(default=None)):
    owner = user_id(request, x_demo_user)
    itineraries = await request.app.state.db.fetchall(
        "SELECT id,title,details_json,created_at FROM itinerary_items WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (owner,),
    )
    reminders = await request.app.state.db.fetchall(
        "SELECT id,title,remind_at,status,created_at FROM reminders WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (owner,),
    )
    return {"itineraries": itineraries, "reminders": reminders}


if frontend_dist.exists():
    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str):
        if full_path == "api" or full_path.startswith(("api/", "assets/", ".")):
            candidate = frontend_dist / full_path
            if not full_path.startswith("assets/") or not candidate.is_file() or frontend_dist not in candidate.resolve().parents:
                raise HTTPException(404, "资源不存在")
        candidate = frontend_dist / full_path
        if candidate.is_file() and frontend_dist in candidate.resolve().parents:
            return FileResponse(candidate)
        index = frontend_dist / "index.html"
        if index.exists():
            return FileResponse(index)
        raise HTTPException(404, "Frontend 尚未构建。请在 frontend 目录执行 npm run dev。")
