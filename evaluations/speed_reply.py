"""Existing respond node and isolated live-weather graphs, never the running app."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import monotonic

from car_agent.adapters.mcp_client import LocalMCPClient
from car_agent.config import Settings
from evaluations.planner_ab import MeteredModel
from evaluations.runtime import RuntimeLab
from evaluations.speed_common import begin, finish, paired
from evaluations.speed_models import model


def eligible(state):
    return (state.get("domain") in {"weather", "navigation", "music", "local_life", "planning"}
            and not state.get("handoff_summary")
            and not re.search("为什么|解释|分析|比较|推荐|建议|怎么办", state.get("original_query", "")))


class StreamMeter(MeteredModel):
    async def astream(self, messages):
        started, first, usage = monotonic(), None, None
        error = None
        try:
            async for chunk in self.model.astream(messages):
                if getattr(chunk, "usage_metadata", None):
                    usage = chunk.usage_metadata
                if getattr(chunk, "content", None) and first is None:
                    first = round((monotonic() - started) * 1000, 3)
                yield chunk
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self.attempts.append({"stage": "response", "elapsed_ms": round((monotonic() - started) * 1000, 3),
                                  "first_text_ms": first, "usage": usage, "error_type": error})


def state(domain, query, result, *, action=None, slots=None, operation=None, status="running"):
    return {"domain": domain, "original_query": query, "tool_result": result,
            "operation_result": operation or {}, "slots": slots or {}, "status": status,
            "action": action or {"weather": "weather_lookup", "navigation": "navigation_search", "music": "music_search", "local_life": "local_search"}.get(domain),
            "mode": "live", "messages": [], "user_id": "speed-lab"}


def response_cases():
    cases = []
    for i, city in enumerate(("无锡", "杭州", "南京", "苏州", "上海", "北京")):
        result = {"ok": True, "city": city, "date": "2026-10-28", "day_weather": "晴", "low": "15", "high": "25", "source": "controlled_fixture"}
        cases.append({"id": f"reply-weather-{i}", "state": state("weather", f"查{city}明天天气", result), "required": [city, "晴", "15", "25"], "expected_status": "completed"})
    for i, (title, singer) in enumerate((("晴天", "周杰伦"), ("爱很简单", "陶喆"), ("十年", "陈奕迅"))):
        result = {"ok": True, "items": [{"id": str(i), "name": title, "singer": singer, "url": f"https://example.org/song/{i}"}], "playback_supported": False}
        cases.append({"id": f"reply-music-{i}", "state": state("music", f"搜索{singer}的《{title}》", result), "required": [title, singer], "expected_status": "completed"})
    for mode in ("driving", "walking", "bicycling"):
        route = {"distance_m": 2000, "duration_s": 600, "travel_mode": mode, "navigation_url": "https://example.org/navigation"}
        result = {"items": [{"id": "poi", "name": "东南大学无锡校区", "address": "测试地址"}], "route_result": route}
        cases.append({"id": "reply-route-" + mode, "state": state("navigation", "查到东南大学无锡校区的路线", result, slots={"selection": 1, "route_requested": True}), "required": ["东南大学"], "expected_status": "completed"})
    for i, name in enumerate(("测试餐厅甲", "测试影院乙", "测试KTV丙")):
        result = {"items": [{"id": str(i), "name": name, "address": "测试路1号", "rating": None, "cost": None}], "booking_supported": False}
        cases.append({"id": f"reply-local-{i}", "state": state("local_life", "查看" + name, result), "required": [name], "expected_status": "completed"})
    cases.extend([
        {"id": "reply-empty", "state": state("music", "搜索指定歌曲", {"ok": True, "items": []}), "required_any": ["没有", "未找到", "暂无", "未能"], "expected_status": "completed"},
        {"id": "reply-write-saved", "state": state("planning", "保存周末散步行程", {}, action="save_itinerary", operation={"status": "saved", "item_id": "fixture-id", "title": "周末散步"}), "required": ["周末散步", "保存"], "expected_status": "completed"},
        {"id": "reply-reminder", "state": state("planning", "创建喝水提醒", {}, action="create_reminder", operation={"status": "scheduled", "reminder_id": "fixture-r", "title": "喝水", "remind_at": "2026-10-28T14:00:00+08:00"}), "required": ["喝水", "提醒"], "expected_status": "completed"},
        {"id": "reply-error", "state": {**state("weather", "查无锡天气", {"error": "查询超时"}, status="failed"), "error": "查询超时"}, "required_any": ["没有完成", "未完成"], "expected_status": "failed"},
        {"id": "reply-bad-route", "state": state("navigation", "查路线", {"items": [{"name": "东南大学"}], "route_result": {}}, slots={"route_requested": True}), "required_any": ["没有完成", "未完成"], "expected_status": "failed"},
    ])
    return cases


def grade_reply(case, result):
    errors = []
    text = result.get("answer", "")
    if result.get("status") != case["expected_status"]:
        errors.append("status")
    for term in case.get("required", []):
        if term not in text:
            errors.append("missing:" + term)
    if case.get("required_any") and not any(term in text for term in case["required_any"]):
        errors.append("missing_negative_notice")
    # Screening rules, not a substitute for a human factuality/richness rubric.
    if re.search(r"(?<!未)(?<!不)(?<!没有)(已播放|正在播放|导航已启动|已启动导航|已订座|已订票)", text):
        errors.append("unsupported_completion")
    return {"passed": not errors, "errors": errors}


async def drain(lab, inputs, config):
    events = []
    async for event in lab.graph.astream(inputs, config=config, context=lab.context, stream_mode="custom"):
        events.append(event)
    snapshot = await lab.graph.aget_state(config)
    return snapshot, events


async def response(args):
    settings = Settings()
    base = model(settings)
    cases = response_cases()
    manifest = begin(args.output, "existing_reply_template_vs_model_polish", cases,
                     source="unchanged respond node; controlled receipts; real baseline response model",
                     upstream="starts from checkpoint before respond; no intent/tool time measured", model=settings.model_name,
                     candidate="response role None only for eligible simple business response", concurrency=1)
    async with RuntimeLab() as lab:
        async def call(case, arm):
            meter = StreamMeter(base)
            lab.context.llm = meter
            lab.context.model_roles = {"response": None} if arm == "candidate" and eligible(case["state"]) else {}
            config = {"configurable": {"thread_id": case["id"] + arm}}
            initial = {**deepcopy(case["state"]), "task_id": case["id"] + arm,
                       "session_id": case["id"] + arm, "request_id": case["id"] + arm}
            await lab.graph.aupdate_state(config, initial, as_node=initial["domain"] + "_agent")
            snapshot, events = await drain(lab, None, config)
            return {"answer": snapshot.values.get("answer"), "status": snapshot.values.get("status"),
                    "verification": snapshot.values.get("execution_verification"), "events": events,
                    "attempts": meter.attempts, "grade": grade_reply(case, snapshot.values)}

        rows = await paired(args.output, cases, call)
    result = finish(args.output, manifest, rows, scope="actual post-tool respond stage, controlled receipts; not end-to-end",
                    excluded=["open-ended chat", "medical advice", "recommendation/explanation requests", "real business writes", "human style scoring"])
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


FLOW_CASES = [{"id": f"live-weather-{i}", "query": query, "city": city}
              for i, (city, query) in enumerate((("无锡", "查无锡今天天气"), ("杭州", "今天杭州天气怎么样"),
                                                ("南京", "查一下南京今天的天气"), ("无锡", "无锡今天的天气如何"),
                                                ("杭州", "查询杭州今天的天气"), ("南京", "南京今日天气怎么样")))]


async def flow(args):
    settings = Settings()
    manifest = begin(args.output, "isolated_live_weather_full_graph", FLOW_CASES,
                     source="real intent model, actual graph/checkpoint/queue, live MCP Amap weather",
                     baseline="default reasoning intent + response model", candidate="none reasoning intent + existing evidence template",
                     concurrency=1, MCP="one pre-opened persistent stdio connection; startup excluded", model=settings.model_name)
    base, fast = model(settings), model(settings, True)
    with TemporaryDirectory(prefix="lifeplus-speed-mcp-") as temporary:
        env = dict(os.environ)
        env["LIFEPLUS_DATA_PATH"] = str(Path(temporary) / "tools.sqlite3")
        if settings.amap_maps_api_key:
            env["AMAP_MAPS_API_KEY"] = settings.amap_maps_api_key
        mcp = LocalMCPClient(env)
        await mcp.open()
        try:
            async with RuntimeLab() as lab:
                async def dispatch(name, arguments, task_id):
                    if name != "maps_weather":
                        raise AssertionError("This live probe permits weather reads only")
                    return await mcp.call(name, arguments)

                lab.handler = dispatch

                async def call(case, arm):
                    lab.calls.clear()
                    meter = StreamMeter(base if arm == "baseline" else fast)
                    lab.context.llm = meter
                    lab.context.model_roles = {"response": None} if arm == "candidate" else {}
                    task_id = case["id"] + arm
                    initial = {"messages": [{"role": "user", "content": case["query"]}], "user_id": "speed-lab", "session_id": task_id,
                               "task_id": task_id, "request_id": task_id, "mode": "live", "original_query": case["query"], "status": "running", "slots": {}}
                    snapshot, events = await drain(lab, initial, {"configurable": {"thread_id": task_id}})
                    value = snapshot.values
                    result = value.get("tool_result", {})
                    errors = []
                    if value.get("status") != "completed": errors.append("not_completed")
                    if result.get("source") != "高德天气" or result.get("simulated"): errors.append("not_real_weather")
                    if not case["city"] in str(result.get("city", "")): errors.append("wrong_city")
                    if not value.get("execution_verification", {}).get("contract_valid"): errors.append("invalid_receipt")
                    if case["city"] not in value.get("answer", ""): errors.append("answer_city_missing")
                    if not lab.calls or any(c["tool"] != "maps_weather" for c in lab.calls): errors.append("tool_selection")
                    return {"answer": value.get("answer"), "status": value.get("status"), "tool_result": result,
                            "calls": list(lab.calls), "events": events, "attempts": meter.attempts,
                            "grade": {"passed": not errors, "errors": errors}}

                rows = await paired(args.output, FLOW_CASES, call, timeout=90)
        finally:
            await mcp.close()
    result = finish(args.output, manifest, rows, scope="six simple weather single-task graphs; live model and MCP, no UI/TaskCoordinator plan phase",
                    excluded=["multi-task planner", "clarification/resume", "navigation/music/merchant end-to-end", "public network SSE/frontend"])
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("response", "flow"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run((response if args.stage == "response" else flow)(args))


if __name__ == "__main__":
    main()
