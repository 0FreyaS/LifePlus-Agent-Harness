"""Four-task live HTTP acceptance; only summary.json is suitable for publication.

Run with --output-dir NEW_DIRECTORY. Raw evidence is kept in private data/.
SHOWCASE_USERNAME and SHOWCASE_PASSWORD optionally enable HTTP Basic auth.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
import math
import os
from pathlib import Path
import time
import uuid

import httpx

QUERY = (
    "请同时完成以下四个子任务，必须恰好拆成四项："
    "1. 检索杭州东站这个车站地点的经纬度，不是饭店等商家检索；"
    "2. 查询无锡今天的天气；3. 搜索周杰伦的歌曲；"
    "4. 第一项选定地点后，以第一项所选地点的坐标作为origin，步行规划到杭州西湖的路线。"
    "前三项互相独立，同时执行；只有第四项依赖第一项，不能猜起点，也不能使用默认起点。"
    "有多个候选时等待我选择，不要预先设置selection；不创建提醒、不保存行程或执行其他写入。"
)
ROLES = ("place", "weather", "music", "route")
DOMAINS = dict(zip(ROLES, ("navigation", "weather", "music", "navigation")))
TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}
STATUSES = TERMINAL | {"queued", "blocked", "running", "waiting_user"}
SOURCES = {"maps_search": "高德地图", "maps_weather": "高德天气",
           "search_music": "QQ音乐", "maps_route": "高德地图"}
CHECK_NAMES = (
    "evidence_schema_valid", "capture_finished", "four_tasks_three_roots_one_dependency",
    "all_completed", "live_model_and_mcp", "trace_order_valid", "dependency_completed_before_start",
    "independent_execution_overlap", "place_search_receipt", "weather_today_receipt",
    "music_search_receipt", "first_candidates_only", "route_origin_bound_to_selected_place",
    "walking_to_west_lake", "no_business_writes",
)
PUBLIC_POIS = {"杭州东站"} | {
    f"杭州东站{suffix}" for suffix in ("-东广场", "-西广场", "(火车站)", "（火车站）",
                                      "(地铁站)", "（地铁站）", "-进站口", "-出站口")}


def _plan(child: dict) -> dict:
    value = child.get("plan_json") or {}
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _roles(task: dict) -> dict:
    result = {}
    if not isinstance(task, dict) or not isinstance(task.get("children", []), list):
        return {}
    for child in task.get("children", []):
        if not isinstance(child, dict):
            return {}
        spec = _plan(child)
        domain, action = spec.get("domain"), spec.get("action")
        if not isinstance(domain, str) or not isinstance(action, str):
            return {}
        role = ("weather" if domain == "weather" and action == "weather_lookup" else
                "music" if domain == "music" and action == "music_search" else
                "route" if domain == "navigation" and action in {"navigation_search", "navigation_route"}
                and spec.get("depends_on") else
                "place" if domain == "navigation" and action == "navigation_search" else None)
        if role is None or role in result:
            return {}
        result[role] = child
    return result


def _stamp(event: dict) -> float | None:
    try:
        dt = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _before(left: dict, right: dict) -> bool:
    a, b = _stamp(left), _stamp(right)
    return (a is not None and b is not None and a <= b
            and isinstance(left.get("seq"), int) and isinstance(right.get("seq"), int)
            and left["seq"] < right["seq"])


def _events(events: list, child: dict, kind: str) -> list:
    return [e for e in events if e.get("child_task_id") == child.get("id") and e.get("type") == kind]


def _calls(events: list, child: dict, tool: str) -> list:
    calls = []
    for done in _events(events, child, "tool.completed"):
        result = done.get("result") or {}
        if (done.get("tool_name") != tool or not isinstance(result, dict)
                or result.get("error") or result.get("ok") is False
                or result.get("simulated") or result.get("requires_clarification")
                or result.get("source") != SOURCES[tool]):
            continue
        starts = [start for start in _events(events, child, "tool.started")
                  if start.get("tool_name") == tool and start.get("job_id")
                  and start["job_id"] == done.get("job_id")
                  and start.get("attempt") == done.get("attempt") and _before(start, done)]
        if starts:
            calls.append((starts[-1], done))
    return calls


def _coordinate(value) -> tuple | None:
    try:
        parts = value.split(",") if isinstance(value, str) else value
        if len(parts) != 2:
            return None
        lon, lat = map(float, parts)
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            return lon, lat
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _same_point(left, right) -> bool:
    a, b = _coordinate(left), _coordinate(right)
    return a is not None and b is not None and all(abs(x - y) <= 1e-6 for x, y in zip(a, b))


def selection_evidence(events: list, child: dict, pending: dict) -> dict | None:
    """Return candidate one only when the tool actually supplied this candidate list."""
    candidates = pending.get("candidates")
    if (pending.get("kind") != "selection" or not pending.get("id") or pending.get("expired")
            or not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict)):
        return None
    for event in reversed(events):
        if event.get("child_task_id") != child.get("id"):
            continue
        if event.get("type") not in {"tool.completed", "tool.clarification_required"}:
            continue
        if event.get("tool_name") not in SOURCES:
            continue
        result = event.get("result") or {}
        if not isinstance(result, dict) or result.get("simulated"):
            continue
        if event["type"] == "tool.completed" and (
                result.get("source") != SOURCES[event["tool_name"]]
                or result.get("error") or result.get("ok") is False):
            continue
        offered = result.get("candidates") if event["type"] == "tool.clarification_required" else result.get("items")
        if offered != candidates:
            continue
        if any(start.get("job_id") == event.get("job_id") and start.get("job_id")
               and start.get("tool_name") == event.get("tool_name")
               and start.get("attempt") == event.get("attempt")
               and _before(start, event) for start in _events(events, child, "tool.started")):
            return {"task_id": child["id"], "interrupt_id": pending["id"], "index": 1,
                    "item": deepcopy(candidates[0]), "candidates": deepcopy(candidates),
                    "job_id": event["job_id"], "offered_seq": event["seq"]}
    return None


def _selected(report: dict, child: dict, call: tuple) -> dict | None:
    _, done = call
    items = done["result"].get("items") or []
    if not items or not isinstance(items[0], dict):
        return None
    records = [s for s in report.get("candidate_selections", []) if s.get("task_id") == child["id"]
               and s.get("job_id") == done.get("job_id") and s.get("index") == 1
               and s.get("item") == items[0] and s.get("candidates") == items]
    events = report.get("events", [])
    # user.resumed intentionally omits the answer; the select node records the index.
    resumed = bool(records) and any(
        e.get("node") == f"{_plan(child).get('domain')}.select" and e.get("selection") == 1
        and _before(done, e) and any(r.get("action") == "answer" and _before(done, r)
                                  and _before(r, e) for r in _events(events, child, "user.resumed"))
        for e in _events(events, child, "node.completed"))
    # A single result is selected by the graph itself, without a resume request.
    automatic = len(items) == 1 and any(
        e.get("node") == f"{_plan(child).get('domain')}.select" and e.get("status") == "automatic"
        and _before(done, e) for e in _events(report.get("events", []), child, "node.completed"))
    return items[0] if resumed or automatic else None


def _segments(events: list, child: dict) -> list:
    segments, start = [], None
    for event in events:
        if event.get("child_task_id") != child.get("id"):
            continue
        kind = event.get("type")
        if kind == "subtask.task.started":
            start = event
        elif kind in {"subtask.interrupt.required", "subtask.task.completed", "subtask.task.failed",
                      "subtask.task.cancelled", "subtask.task.interrupted"} and start:
            if _before(start, event):
                segments.append((start, event))
            start = None
    return segments


def _overlap(a: tuple, b: tuple) -> bool:
    return (_before(a[0], b[1]) and _before(b[0], a[1])
            and max(_stamp(a[0]), _stamp(b[0])) < min(_stamp(a[1]), _stamp(b[1])))


def validate_report(report: dict) -> dict[str, bool]:
    """Validate captured evidence without I/O or trusting completed labels alone."""
    try:
        return _validate_report(report)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return dict.fromkeys(CHECK_NAMES, False)


def _validate_report(report: dict) -> dict[str, bool]:
    task, events = report.get("task") or {}, report.get("events") or []
    roles = _roles(task)
    specs = {role: _plan(child) for role, child in roles.items()}
    complete_roles = set(roles) == set(ROLES)
    children = task.get("children", [])
    ids = [c.get("id") for c in children]
    plan_ids = [_plan(c).get("id") for c in children]
    graph = (complete_roles and len(children) == 4 and len(set(ids)) == 4 and all(ids)
             and len(set(plan_ids)) == 4 and all(plan_ids)
             and all(not specs[r].get("depends_on") for r in ROLES[:3])
             and specs["route"].get("depends_on") == [specs["place"].get("id")])
    seqs = [e.get("seq") for e in events]
    trace_valid = (bool(events) and all(type(s) is int and s > 0 for s in seqs)
                   and seqs == sorted(set(seqs)) and all(_stamp(e) is not None for e in events))
    health = report.get("health") or {}
    model_starts = [e for e in events if e.get("type") == "model.attempt.started"
                    and e.get("stage") == "plan" and not e.get("child_task_id") and e.get("span_id")]
    model = any(e.get("type") == "model.attempt.completed" and e.get("stage") == "plan"
                and not e.get("child_task_id") and e.get("span_id") == start["span_id"]
                and _before(start, e) for start in model_starts for e in events)
    checks = {
        "evidence_schema_valid": True,
        "capture_finished": report.get("outcome") == "completed",
        "four_tasks_three_roots_one_dependency": bool(graph),
        "all_completed": bool(complete_roles and task.get("status") == "completed"
                              and all(c.get("status") == "completed"
                                      and _events(events, c, "subtask.task.completed") for c in children)),
        "live_model_and_mcp": bool(model and health.get("model_configured") is True
                                   and health.get("maps_configured") is True
                                   and health.get("tools_backend") == "real_mcp"
                                   and task.get("mode") == "live"
                                   and all(c.get("mode") == "live" for c in children)),
        "trace_order_valid": bool(trace_valid),
        "dependency_completed_before_start": False,
        "independent_execution_overlap": False,
        "place_search_receipt": False,
        "weather_today_receipt": False,
        "music_search_receipt": False,
        "first_candidates_only": False,
        "route_origin_bound_to_selected_place": False,
        "walking_to_west_lake": False,
        "no_business_writes": all(e.get("tool_name") in SOURCES for e in events
                                  if e.get("type") == "tool.started")
                              and not any(e.get("action") in {"approve", "reject"}
                                          or e.get("kind") == "approval" for e in events),
    }
    if not complete_roles:
        return checks
    place, weather, music, route = (roles[r] for r in ROLES)
    done = _events(events, place, "subtask.task.completed")
    begun = _events(events, route, "subtask.task.started")
    checks["dependency_completed_before_start"] = bool(graph and done and begun
                                                        and all(_before(done[-1], e) for e in begun))
    checks["independent_execution_overlap"] = bool(trace_valid and any(
        _overlap(a, b) for left, right in combinations(ROLES[:3], 2)
        for a in _segments(events, roles[left]) for b in _segments(events, roles[right])))
    selected = None
    for call in _calls(events, place, "maps_search"):
        args = call[0].get("arguments") or {}
        item = _selected(report, place, call)
        if ("杭州东站" in str(args.get("keywords", "")) and item
                and item.get("name") in PUBLIC_POIS and _coordinate(item.get("location"))):
            selected = item
            checks["place_search_receipt"] = True
    for start, end in _calls(events, weather, "maps_weather"):
        args, result = start.get("arguments") or {}, end["result"]
        checks["weather_today_receipt"] |= bool("无锡" in str(args.get("city", ""))
            and "无锡" in str(result.get("city", "")) and result.get("day_weather")
            and result.get("date") == report.get("today") and report.get("today")
            and args.get("forecast_date", report["today"]) in {"", report["today"]})
    for start, end in _calls(events, music, "search_music"):
        checks["music_search_receipt"] |= bool("周杰伦" in str((start.get("arguments") or {}).get("keyword", ""))
            and any("周杰伦" in str(item.get("singer", "")) for item in end["result"].get("items", [])
                    if isinstance(item, dict)) and _selected(report, music, (start, end)))
    selections = report.get("candidate_selections", [])
    checks["first_candidates_only"] = bool(selected and all(
        s.get("index") == 1 and s.get("candidates") and s.get("item") == s["candidates"][0]
        and any(e.get("child_task_id") == s.get("task_id") and e.get("seq") == s.get("offered_seq")
                and e.get("job_id") == s.get("job_id")
                and ((e.get("result") or {}).get("items") == s["candidates"]
                     or (e.get("result") or {}).get("candidates") == s["candidates"]) for e in events)
        for s in selections) and not any(
            e.get("type") == "user.resumed" and (e.get("action") != "answer"
                or ("value" in e and e["value"] != {"index": 1}))
            or (e.get("type") == "node.completed" and "selection" in e and e["selection"] != 1)
            for e in events))
    binding = specs["route"].get("bind_slots", {}).get("origin")
    prepared = specs["route"].get("prepared_decision", {}).get("slots", {})
    receipt = report.get("route") or {}
    for start, end in _calls(events, route, "maps_route"):
        args, result = start.get("arguments") or {}, end["result"]
        checks["route_origin_bound_to_selected_place"] |= bool(selected and graph
            and binding == f"{specs['place']['id']}.selected.location"
            and _same_point(selected["location"], prepared.get("origin"))
            and _same_point(selected["location"], args.get("origin"))
            and _same_point(selected["location"], result.get("origin"))
            and receipt.get("task_id") == route["id"] and receipt.get("job_id") == end.get("job_id")
            and receipt.get("source") == "高德地图" and not receipt.get("simulated")
            and _same_point(selected["location"], receipt.get("origin")))
        destination = str(args.get("destination", ""))
        destination_items = [item for call in _calls(events, route, "maps_search")
                             for item in call[1]["result"].get("items", [])
                             if isinstance(item, dict) and "西湖" in str(item.get("name", ""))]
        target = ("西湖" in destination and "杭州" in (destination + str(args.get("city", "")))
                  or any(_same_point(destination, item.get("location")) for item in destination_items))
        checks["walking_to_west_lake"] |= bool(target and args.get("travel_mode") == "walking"
            and result.get("travel_mode") == receipt.get("travel_mode") == "walking"
            and _same_point(result.get("destination"), receipt.get("destination"))
            and all(isinstance(result.get(k), (int, float)) and math.isfinite(result[k]) and result[k] > 0
                    for k in ("distance_m", "duration_s")))
    return checks


def public_summary(report: dict) -> dict:
    """A publication allowlist: never copy queries, names, IDs or errors from responses."""
    checks = validate_report(report)
    roles = _roles(report.get("task") or {})
    role_by_plan = {_plan(child).get("id"): role for role, child in roles.items()
                    if isinstance(_plan(child).get("id"), str)}
    events = report.get("events") or []
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
    selected_name = None
    if checks["place_search_receipt"]:
        for call in _calls(events, roles["place"], "maps_search"):
            item = _selected(report, roles["place"], call)
            if item and item.get("name") in PUBLIC_POIS:
                selected_name = item["name"]
    branches = []
    for role in ROLES:
        child = roles.get(role, {})
        segments = _segments(events, child) if child else []
        elapsed = sum(_stamp(end) - _stamp(start) for start, end in segments)
        status = child.get("status")
        dependencies = _plan(child).get("depends_on", [])
        dependencies = ([role_by_plan.get(source, "unknown") if isinstance(source, str) else "unknown"
                         for source in dependencies] if isinstance(dependencies, list) else ["unknown"])
        branches.append({"branch": role, "domain": DOMAINS[role],
                         "status": status if isinstance(status, str) and status in STATUSES else "unknown",
                         "depends_on": dependencies,
                         "elapsed_seconds": round(elapsed, 3) if segments else None})
    elapsed = report.get("elapsed_seconds")
    return {"query": QUERY, "status": "passed" if all(checks.values()) else "failed",
            "elapsed_seconds": round(elapsed, 3) if isinstance(elapsed, (int, float))
            and math.isfinite(elapsed) and elapsed >= 0 else None,
            "branches": branches,
            "selected_public_poi_name": selected_name,
            "checks": checks}


class EvidenceError(Exception):
    pass


async def capture(base_url: str, *, timeout: float = 180, poll_interval: float = 0.5,
                  auth: httpx.BasicAuth | None = None, transport=None) -> dict:
    """Perform one live natural-language request. transport is for offline tests only."""
    if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("timeout and poll_interval must be finite and positive")
    report = {"query": QUERY, "task": {}, "events": [], "candidate_selections": [], "snapshots": [],
              "today": datetime.now(timezone(timedelta(hours=8))).date().isoformat(), "outcome": "failed"}
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, auth=auth, timeout=min(timeout, 20),
                                 transport=transport, trust_env=False,
                                 headers={"X-Demo-User": "showcase-smoke"}) as client:
        async def request(method, path, *, expected=dict, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, expected):
                raise EvidenceError("invalid_response_schema")
            return value

        async def trace():
            events = await request("GET", f"/api/tasks/{report['task_id']}/trace", expected=list)
            if any(not isinstance(e, dict) or not isinstance(e.get("result", {}), dict) for e in events):
                raise EvidenceError("invalid_trace_schema")
            report["events"] = events

        handled = set()
        try:
            async with asyncio.timeout(timeout):
                report["health"] = await request("GET", "/api/health")
                health = report["health"]
                if not (health.get("model_configured") and health.get("maps_configured")
                        and health.get("tools_backend") == "real_mcp"):
                    raise EvidenceError("live_backend_unavailable")
                session = await request("POST", "/api/sessions")
                report["session_id"] = session["id"]
                response = await request("POST", f"/api/sessions/{session['id']}/messages", json={
                    "message": QUERY, "mode": "live", "request_id": str(uuid.uuid4())})
                report["task_id"] = response["task_id"]
                while True:
                    task = await request("GET", f"/api/tasks/{report['task_id']}")
                    if (not isinstance(task.get("children", []), list)
                            or any(not isinstance(c, dict) for c in task.get("children", []))):
                        raise EvidenceError("invalid_task_schema")
                    report["task"] = task
                    states = [(c["id"], c["status"]) for c in task.get("children", [])]
                    snapshot = {"status": task.get("status"), "states": states}
                    if not report["snapshots"] or snapshot != report["snapshots"][-1]["state"]:
                        report["snapshots"].append({"seconds": round(time.monotonic() - started, 3), "state": snapshot})
                    if len(task.get("children", [])) > 4:
                        raise EvidenceError("unexpected_subtask_count")
                    for child in [task, *task.get("children", [])]:
                        pending = child.get("interrupt") or {}
                        if not pending or pending.get("id") in handled:
                            continue
                        if pending.get("kind") != "selection":
                            raise EvidenceError("unsupported_interrupt")
                        await trace()
                        evidence = selection_evidence(report["events"], child, pending)
                        if evidence is None:
                            raise EvidenceError("unverified_candidates")
                        await request("POST", f"/api/tasks/{child['id']}/resume", json={
                            "interrupt_id": pending["id"], "action": "answer", "value": {"index": 1}})
                        handled.add(pending["id"])
                        report["candidate_selections"].append(evidence)
                    if task.get("status") in TERMINAL:
                        report["outcome"] = "completed" if task["status"] == "completed" else "task_failed"
                        break
                    await asyncio.sleep(poll_interval)
                await trace()
                route = _roles(task).get("route")
                if route:
                    report["route"] = await request("GET", f"/api/tasks/{route['id']}/route")
        except TimeoutError:
            report["outcome"] = "timeout"
        except EvidenceError as exc:
            report["outcome"] = str(exc)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError):
            # Exception strings can contain URLs and credentials; never persist them.
            report["outcome"] = "http_or_schema_error"
        if report["outcome"] != "completed" and report.get("task_id"):
            try:
                async with asyncio.timeout(5):
                    await request("POST", f"/api/tasks/{report['task_id']}/cancel")
                    await trace()
            except (TimeoutError, httpx.HTTPError, KeyError, TypeError, ValueError, AttributeError, EvidenceError):
                pass
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8012")
    parser.add_argument("--output-dir", "--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    if any(not math.isfinite(v) or v <= 0 for v in (args.timeout, args.poll_interval)):
        parser.error("timeout and poll-interval must be finite and positive")
    try:
        url = httpx.URL(args.base_url)
    except httpx.InvalidURL:
        parser.error("invalid base-url")
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        parser.error("base-url must be HTTP(S) without embedded credentials")
    username, password = os.environ.get("SHOWCASE_USERNAME"), os.environ.get("SHOWCASE_PASSWORD")
    if (username is None) != (password is None):
        parser.error("set both SHOWCASE_USERNAME and SHOWCASE_PASSWORD, or neither")
    auth = httpx.BasicAuth(username, password) if username is not None else None
    try:
        args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        parser.error("output-dir must not already exist; existing evidence is preserved")
    private = args.output_dir / "private data"
    private.mkdir(mode=0o700)
    report = asyncio.run(capture(str(url), timeout=args.timeout, poll_interval=args.poll_interval, auth=auth))
    summary = public_summary(report)
    with open(private / "report.json", "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    with (args.output_dir / "summary.json").open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
