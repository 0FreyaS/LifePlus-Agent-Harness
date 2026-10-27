"""Full production prompt and validators, default reasoning vs explicit none."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from langchain_openai import ChatOpenAI

from car_agent.config import Settings
from car_agent.harness import resolve_intent
from car_agent.task_planning import plan_tasks
from evaluations.planner_ab import MeteredModel, NOW
from evaluations.planner_cases import CASES, grade, value_equal
from evaluations.speed_common import begin, finish, paired


def model(settings, fast=False):
    return ChatOpenAI(base_url=settings.model_base_url, api_key=settings.model_api_key,
                      model=settings.model_name, temperature=0, timeout=25, max_retries=0,
                      max_tokens=settings.model_max_output_tokens, **({"reasoning_effort": "none"} if fast else {}))


INTENT_CASES = [
    {"id": "i01", "query": "查询无锡2026年10月28日的天气", "domain": "weather", "action": "weather_lookup", "slots": {"city": "无锡", "forecast_date": "2026-10-28"}},
    {"id": "i02", "query": "搜索周杰伦的歌曲", "domain": "music", "action": "music_search", "slots": {"keyword": "周杰伦"}},
    {"id": "i03", "query": "帮我找一下陶喆的歌", "domain": "music", "action": "music_search", "slots": {"keyword": "陶喆"}},
    {"id": "i04", "query": "从无锡站步行到东南大学无锡校区", "domain": "navigation", "action": "navigation_search", "slots": {"origin": "无锡站", "destination": "东南大学无锡校区", "travel_mode": "walking"}},
    {"id": "i05", "query": "从杭州东站骑车到西湖", "domain": "navigation", "action": "navigation_search", "slots": {"origin": "杭州东站", "destination": "西湖", "travel_mode": "bicycling"}},
    {"id": "i06", "query": "从南京站开车到南京南站", "domain": "navigation", "action": "navigation_search", "slots": {"origin": "南京站", "destination": "南京南站", "travel_mode": "driving"}},
    {"id": "i07", "query": "找无锡站附近人均不超过80元的饭店", "domain": "local_life", "action": "local_search", "slots": {"category": "restaurant", "max_cost": 80}},
    {"id": "i08", "query": "查杭州东站附近的电影院", "domain": "local_life", "action": "local_search", "slots": {"category": "cinema"}},
    {"id": "i09", "query": "找南京新街口附近评分至少4分的咖啡店", "domain": "local_life", "action": "local_search", "slots": {"category": "cafe", "min_rating": 4}},
    {"id": "i10", "query": "附近有KTV吗", "domain": "local_life", "action": "local_search", "slots": {"category": "ktv"}, "absent": ["anchor", "city"]},
    {"id": "i11", "query": "列出我的提醒", "domain": "planning", "action": "list_reminders", "slots": {}},
    {"id": "i12", "query": "查看我保存的行程", "domain": "planning", "action": "list_itineraries", "slots": {}},
    {"id": "i13", "query": "提醒我喝水", "domain": "planning", "action": "create_reminder", "slots": {}, "absent": ["remind_at"]},
    {"id": "i14", "query": "取消一个提醒", "domain": "planning", "action": "cancel_reminder", "slots": {}, "absent": ["reminder_id"]},
    {"id": "i15", "query": "把周末西湖散步保存到行程", "domain": "planning", "action": "save_itinerary", "slots": {}},
    {"id": "i16", "query": "明天天气怎么样", "domain": "weather", "action": "weather_lookup", "slots": {}, "absent": ["city"]},
    {"id": "i17", "query": "你是谁", "domain": "chat", "action": "chat_answer", "slots": {}},
    {"id": "i18", "query": "给我讲个笑话", "domain": "chat", "action": "chat_answer", "slots": {}},
    {"id": "i19", "query": "导航到东南大学", "domain": "navigation", "action": "navigation_search", "slots": {"destination": "东南大学"}, "absent": ["origin"]},
    {"id": "i20", "query": "查商家POI编号B0TEST123的详情", "domain": "local_life", "action": "local_detail", "slots": {"poi_id": "B0TEST123"}},
]


def grade_intent(case, result):
    errors = []
    for key in ("domain", "action"):
        if result.get(key) != case[key]:
            errors.append(key)
    for key, value in case["slots"].items():
        if not value_equal(result.get("slots", {}).get(key), value):
            errors.append("slot:" + key)
    for key in case.get("absent", []):
        if result.get("slots", {}).get(key) not in (None, ""):
            errors.append("inferred:" + key)
    return {"passed": not errors, "errors": errors}


async def run(args):
    settings = Settings()
    models = {"baseline": model(settings), "candidate": model(settings, fast=True)}
    cases = CASES if args.stage == "plan" else INTENT_CASES
    manifest = begin(args.output, "reasoning_none_" + args.stage, cases, model=settings.model_name,
                     baseline_reasoning="provider_default_unset", candidate_reasoning="none",
                     prompt="full unchanged production prompt", tokens_cap=settings.model_max_output_tokens,
                     json_repair_limit=1, concurrency=1, frozen_plan_time=NOW.isoformat(),
                     data_note="plan reuses frozen 30-case contract set without tuning; intent is 20 newly labeled cases")

    async def call(case, arm):
        meter = MeteredModel(models[arm])
        try:
            if args.stage == "plan":
                result = await plan_tasks(case["query"], "live", meter, case["memory_brief"], variant="baseline", now=NOW)
                actual, score = result.model_dump() if result else None, grade(case, result)
            else:
                result = await resolve_intent({"original_query": case["query"], "mode": "live"}, {},
                                              SimpleNamespace(context=SimpleNamespace(llm=meter)))
                actual, score = result, grade_intent(case, result)
            return {"actual": actual, "grade": score, "attempts": meter.attempts}
        except Exception as exc:
            return {"error_type": type(exc).__name__, "grade": {"passed": False, "errors": [type(exc).__name__]}, "attempts": meter.attempts}

    rows = await paired(args.output, cases, call)
    result = finish(args.output, manifest, rows, scope="real model component benchmark, no business tools executed")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("plan", "intent"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
