"""Authored evaluation scenarios. Generated files are not historical user data."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def build_cases() -> list[dict]:
    cases: list[dict] = []

    def add(group, query, expected, **extra):
        number = sum(row["group"] == group for row in cases) + 1
        cases.append({"id": f"{group}-{number:02d}", "group": group, "query": query,
                      "expected": expected, "source": "authored_synthetic_v1", **extra})

    single = [
        ("无锡今天的天气怎么样", "weather", "weather_lookup"),
        ("明天杭州会下雨吗", "weather", "weather_lookup"),
        ("后天南京气温多少", "weather", "weather_lookup"),
        ("查上海今天的天气", "weather", "weather_lookup"),
        ("深圳明天气温", "weather", "weather_lookup"),
        ("查一下北京今天会不会下雨", "weather", "weather_lookup"),
        ("从无锡站到东南大学无锡校区怎么走", "navigation", "navigation_search"),
        ("从杭州东站步行到西湖规划路线", "navigation", "navigation_search"),
        ("导航去南京南站", "navigation", "navigation_search"),
        ("从苏州站骑行去拙政园", "navigation", "navigation_search"),
        ("从上海虹桥站开车去外滩", "navigation", "navigation_search"),
        ("从北京站到天安门的路线", "navigation", "navigation_search"),
        ("搜索陶喆的歌曲", "music", "music_search"),
        ("找周杰伦的晴天", "music", "music_search"),
        ("搜索陈奕迅的十年", "music", "music_search"),
        ("搜索孙燕姿的遇见", "music", "music_search"),
        ("帮我找林俊杰的江南", "music", "music_search"),
        ("在无锡站附近找饭店", "local_life", "local_search"),
        ("东南大学无锡校区附近有什么KTV", "local_life", "local_search"),
        ("找杭州东站附近的电影院", "local_life", "local_search"),
        ("在南京南站附近推荐咖啡店", "local_life", "local_search"),
        ("查商家POI ID B0FFHONRTZ的详情", "local_life", "local_detail"),
        ("提醒我明天上午九点带伞", "planning", "create_reminder"),
        ("保存行程：周末去无锡", "planning", "save_itinerary"),
        ("查看我的提醒", "planning", "list_reminders"),
        ("查看我的行程清单", "planning", "list_itineraries"),
        ("取消提醒，提醒ID为reminder-123", "planning", "cancel_reminder"),
        ("最近睡不好有什么一般生活建议", "wellbeing", "knowledge_search"),
        ("你好，介绍一下你自己", "chat", "chat_answer"),
        ("讲一个关于春天的小故事", "chat", "chat_answer"),
    ]
    for query, domain, action in single:
        add("single_task", query, {"domain": domain, "action": action}, runner="model")

    # Distinct city/artist and domain combinations; dependencies are separately annotated.
    parallel = [
        ("查无锡天气，同时搜索周杰伦歌曲", ["weather", "music"]),
        ("杭州明天天气，另外找陶喆的歌", ["weather", "music"]),
        ("查南京天气并且查看我的提醒", ["weather", "planning"]),
        ("查上海天气，顺便列出我的行程清单", ["weather", "planning"]),
        ("搜索陈奕迅歌曲，同时查看我的提醒", ["music", "planning"]),
        ("找孙燕姿的歌，以及查看我的行程清单", ["music", "planning"]),
        ("无锡站附近找饭店，同时查无锡天气", ["local_life", "weather"]),
        ("南京南站附近找电影院，顺便搜索林俊杰的歌", ["local_life", "music"]),
        ("找杭州东站附近咖啡店，并且查看我的提醒", ["local_life", "planning"]),
        ("找无锡站附近KTV，同时查上海天气", ["local_life", "weather"]),
        ("从无锡站导航到东南大学无锡校区，同时搜索陶喆歌曲", ["navigation", "music"]),
        ("查杭州天气，并从杭州东站导航到西湖", ["weather", "navigation"]),
        ("从苏州站到拙政园怎么走，顺便列出我的提醒", ["navigation", "planning"]),
        ("查无锡天气、搜索周杰伦歌曲，并且查看我的提醒", ["weather", "music", "planning"]),
        ("查上海天气，同时搜索陈奕迅歌曲，以及查看我的行程清单", ["weather", "music", "planning"]),
        ("从南京站到南京南站怎么走，同时查南京天气和搜索孙燕姿歌曲", ["navigation", "weather", "music"]),
        ("找无锡站附近饭店，查无锡天气，同时查看我的提醒", ["local_life", "weather", "planning"]),
        ("查北京天气，然后搜索周杰伦歌曲，两件事互不依赖", ["weather", "music"]),
        ("先列出我的提醒，然后查杭州天气，两件事互不依赖", ["planning", "weather"]),
        ("搜索林俊杰歌曲，同时找苏州站附近咖啡店，并查看我的行程清单", ["music", "local_life", "planning"]),
    ]
    for query, domains in parallel:
        add("multi_task", query, {"domains": domains, "dependency": "independent"}, runner="model")
    for query, domains in [
        ("查无锡今天天气，然后把查到的天气结果保存到行程", ["weather", "planning"]),
        ("查杭州明天天气，然后把查询结果保存到行程清单", ["weather", "planning"]),
        ("搜索陶喆的歌曲，然后把搜到的歌曲信息保存到行程", ["music", "planning"]),
        ("从无锡站到东南大学无锡校区规划路线，然后把得到的路线保存到行程", ["navigation", "planning"]),
        ("查南京天气，再把查到的天气数据保存为出游行程", ["weather", "planning"]),
    ]:
        add("multi_task", query, {"domains": domains, "dependency": "required"}, runner="model")
    for query, domain in [
        ("在无锡站附近找饭店，按评分排序", "local_life"),
        ("搜索歌曲《一路向北》，歌手周杰伦", "music"),
        ("从120.1482,30.2395到西湖怎么走", "navigation"),
        ("查杭州天气，不用搜索音乐", "weather"),
        ("找东南大学无锡校区附近的咖啡店，人均不超过50元", "local_life"),
    ]:
        add("multi_task", query, {"domains": [domain], "dependency": "independent"}, runner="model", tag="no_over_split")

    slot_cases = [
        ("查无锡明天天气", "weather", {"city": "无锡", "forecast_date": "$tomorrow"}),
        ("杭州后天会下雨吗", "weather", {"city": "杭州", "forecast_date": "$day_after"}),
        ("今天北京天气", "weather", {"city": "北京", "forecast_date": "$today"}),
        ("上海明天的气温", "weather", {"city": "上海", "forecast_date": "$tomorrow"}),
        ("从无锡站到东南大学无锡校区怎么走", "navigation", {"origin": "无锡站", "destination": "东南大学无锡校区"}),
        ("从杭州东站步行到西湖", "navigation", {"origin": "杭州东站", "destination": "西湖", "travel_mode": "walking"}),
        ("从苏州站骑行去拙政园", "navigation", {"travel_mode": "bicycling"}),
        ("从上海虹桥站开车到外滩", "navigation", {"travel_mode": "driving"}),
        ("从120.1482,30.2395到西湖规划路线", "navigation", {"origin": "120.1482,30.2395"}),
        ("搜索陶喆的歌曲", "music", {"keyword": "陶喆"}),
        ("搜索歌曲晴天", "music", {"keyword": "晴天"}),
        ("找歌曲十年", "music", {"keyword": "十年"}),
        ("搜索歌手孙燕姿的作品", "music", {"keyword": "孙燕姿"}),
        ("找无锡站附近2公里的饭店", "local_life", {"category": "restaurant", "radius_m": 2000}),
        ("找无锡站附近800米的咖啡店", "local_life", {"category": "cafe", "radius_m": 800}),
        ("找无锡站附近1.5公里的KTV", "local_life", {"category": "ktv", "radius_m": 1500}),
        ("找杭州东站附近3公里的电影院", "local_life", {"category": "cinema", "radius_m": 3000}),
        ("无锡站附近找饭店，人均不超过80元", "local_life", {"max_cost": 80}),
        ("南京南站附近找咖啡店，评分至少4.5分", "local_life", {"min_rating": 4.5}),
        ("苏州站附近找KTV，按评分排序", "local_life", {"sort_by": "rating"}),
        ("杭州东站附近找电影院，距离最近的优先", "local_life", {"sort_by": "distance"}),
        ("找无锡站附近2公里内饭店，人均最多100元，评分不低于4分", "local_life", {"radius_m": 2000, "max_cost": 100, "min_rating": 4}),
        ("查POI ID B0FFHONRTZ这家商店详情", "local_life", {"poi_id": "B0FFHONRTZ"}),
        ("取消提醒，ID是reminder-abc", "planning", {"reminder_id": "reminder-abc"}),
        ("提醒我明天上午9点带伞", "planning", {"remind_at": "$tomorrow9"}),
        ("提醒我后天下午3点取快递", "planning", {"remind_at": "$day_after15"}),
        ("提醒我明天晚上8点喝水", "planning", {"remind_at": "$tomorrow20"}),
        ("找附近的饭店，我还没告诉你我在哪里", "local_life", {"category": "restaurant", "anchor": None}),
        ("帮我看看天气，我没有提供城市", "weather", {"city": None}),
        ("取消一个提醒，但我还不知道提醒ID", "planning", {"reminder_id": None}),
    ]
    for query, domain, slots in slot_cases:
        add("slot_extraction", query, {"domain": domain, "slots": slots}, runner="model")

    clarification = [
        ("查明天天气", "city"), ("今天会下雨吗", "city"), ("明天气温多少", "city"),
        ("帮我查询天气", "city"), ("后天天气如何", "city"),
        ("导航去东南大学", "origin"), ("带我去无锡站", "origin"),
        ("去南京南站怎么走", "origin"), ("规划去西湖的路线", "origin"), ("导航到外滩", "origin"),
        ("找附近饭店", "anchor"), ("附近有什么KTV", "anchor"),
        ("帮我找附近电影院", "anchor"), ("推荐附近咖啡店", "anchor"),
        ("提醒我喝水", "remind_at"),
        ("查无锡今天的天气", None), ("查杭州明天天气", None), ("南京后天天气如何", None),
        ("北京今天会下雨吗", None), ("上海明天的气温", None),
        ("从无锡站到东南大学无锡校区怎么走", None),
        ("从杭州东站步行到西湖怎么走", None), ("从南京站到南京南站怎么走", None),
        ("从苏州站到拙政园规划路线", None), ("从上海站到外滩怎么走", None),
        ("找无锡站附近饭店", None), ("找南京南站附近KTV", None),
        ("找杭州东站附近电影院", None), ("找苏州站附近咖啡店", None),
        ("提醒我明天上午9点喝水", None),
    ]
    for query, field in clarification:
        add("clarification", query, {"missing_field": field}, runner="model_graph")

    for domain in ("navigation", "music", "local_life"):
        for scenario in ("first", "second", "last", "ordinal", "numeric_text", "invalid_zero", "invalid_large", "invalid_text", "single", "empty"):
            add("candidate_selection", f"{domain}:{scenario}", {"scenario": scenario},
                runner="controlled_graph", domain=domain)
    for action in ("save_itinerary", "create_reminder", "cancel_reminder"):
        for scenario in ("approve", "reject", "unapproved", "bad_key", "reopen_checkpoint",
                         "cancel_inflight", "queue_dedup", "queue_conflict", "late_success", "isolated_threads"):
            add("recovery", f"{action}:{scenario}", {"scenario": scenario}, runner="controlled_runtime", action=action)
    for tool in ("maps_weather", "maps_search", "maps_route", "search_music", "maps_local_search", "maps_place_detail"):
        for scenario in ("permanent", "timeout", "transient", "dependency_failure", "parallel_progress"):
            add("tool_failure", f"{tool}:{scenario}", {"scenario": scenario}, runner="fault_injection", tool=tool)

    for city in ("无锡", "杭州", "南京", "上海", "北京", "苏州"):
        add("real_tools", f"{city}天气", {"tool": "maps_weather", "args": {"city": city}}, runner="real_mcp")
    for city, place in (("无锡", "东南大学无锡校区"), ("杭州", "杭州东站"), ("南京", "南京南站"), ("苏州", "拙政园"), ("上海", "外滩"), ("北京", "北京站")):
        add("real_tools", f"检索{place}", {"tool": "maps_search", "args": {"keywords": place, "city": city}}, runner="real_mcp")
    for artist in ("周杰伦", "陶喆", "陈奕迅", "孙燕姿", "林俊杰", "王菲"):
        add("real_tools", f"搜索{artist}歌曲", {"tool": "search_music", "args": {"keyword": artist, "limit": 2}}, runner="real_mcp")
    for category in ("restaurant", "ktv", "cinema", "cafe"):
        add("real_tools", f"无锡校区周边{category}", {"tool": "maps_local_search", "args": {
            "category": category, "anchor": "东南大学无锡国际校区", "city": "无锡", "radius_m": 5000, "limit": 2}}, runner="real_mcp")
    for mode in ("driving", "walking", "bicycling"):
        add("real_tools", f"杭州{mode}路线", {"tool": "maps_route", "args": {
            "origin": "120.1482,30.2395", "destination": "120.1551,30.2420", "city": "杭州", "travel_mode": mode}}, runner="real_mcp")
    add("real_tools", "跨城30001历史回归", {"tool": "maps_route", "args": {
        "origin": "从无锡市人民医院出发", "destination": "118.795193,32.054213", "city": "南京市", "travel_mode": "driving"}}, runner="real_mcp")
    add("real_tools", "真实商家详情", {"tool": "maps_place_detail", "args": {"poi_id": "B0FFHONRTZ"}}, runner="real_mcp")
    for title in ("评测行程一", "评测行程二"):
        add("real_tools", title, {"tool": "save_itinerary", "args": {"user_id": "evaluation-only", "title": title,
            "details": {"source": "eval-v1"}, "idempotency_key": title}}, runner="real_mcp")
    add("real_tools", "评测用户行程读取", {"tool": "list_itineraries", "args": {"user_id": "evaluation-only"}}, runner="real_mcp")
    counts = Counter(row["group"] for row in cases)
    if len(counts) != 8 or any(count != 30 for count in counts.values()):
        raise ValueError(f"Expected eight groups of 30: {counts}")
    if len({row["id"] for row in cases}) != len(cases):
        raise ValueError("Duplicate case ID")
    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evaluations/data/lifeplus-v1.jsonl")
    args = parser.parse_args()
    cases = build_cases()
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "groups": dict(Counter(row["group"] for row in cases)), "path": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
