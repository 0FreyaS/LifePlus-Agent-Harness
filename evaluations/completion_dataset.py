"""Achievable user goals and an explicit, non-LLM user interaction policy."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def goal(domain, tool, arguments=None, **extra):
    arguments = dict(arguments or {})
    if tool == "maps_weather":
        arguments.setdefault("forecast_date", "$today")
    return {"domain": domain, "tool": tool, "arguments": arguments, **extra}


def build_cases():
    cases = []

    def add(group, query, goals, **policy):
        number = sum(case["group"] == group for case in cases) + 1
        cases.append({"id": f"{group}-{number:02d}", "group": group, "query": query,
            "goals": goals, "user_policy": policy, "source": "authored_completion_v1"})

    for city in ("无锡", "杭州", "南京", "上海", "苏州", "北京", "成都", "深圳"):
        add("single_completion", f"查{city}今天的天气", [goal("weather", "maps_weather", {"city": city})])
    for artist in ("周杰伦", "陶喆", "陈奕迅", "孙燕姿", "林俊杰", "王菲"):
        add("single_completion", f"搜索{artist}的歌曲", [goal("music", "search_music", {"keyword": artist})])
    for anchor in ("无锡站", "杭州东站"):
        for category, label in (("restaurant", "饭店"), ("ktv", "KTV"), ("cinema", "电影院"), ("cafe", "咖啡店")):
            add("single_completion", f"找{anchor}附近的{label}", [goal("local_life", "maps_local_search", {"category": category, "anchor": anchor}, detail=True)])
    for mode, word in (("walking", "步行"), ("driving", "开车"), ("bicycling", "骑行")):
        add("single_completion", f"从杭州东站{word}到杭州西湖怎么走", [goal("navigation", "maps_route", {"travel_mode": mode}, origin_contains="杭州东站")])
    add("single_completion", "保存行程，标题是周末无锡一日游", [goal("planning", "save_itinerary", {"title": "周末无锡一日游"})])
    add("single_completion", "提醒我明天上午9点带伞", [goal("planning", "create_reminder", {"title": "带伞"})])
    add("single_completion", "查看我的提醒", [goal("planning", "list_reminders")])
    add("single_completion", "查看我的行程清单", [goal("planning", "list_itineraries")])
    add("single_completion", "介绍一下歌手陶喆，不用搜索歌曲", [goal("chat", None, answer_contains="陶喆")])

    combinations = [("无锡", "周杰伦"), ("杭州", "陶喆"), ("南京", "陈奕迅"), ("上海", "孙燕姿"), ("苏州", "林俊杰")]
    for city, artist in combinations:
        add("multi_completion", f"查{city}今天的天气，同时搜索{artist}的歌曲", [goal("weather", "maps_weather", {"city": city}), goal("music", "search_music", {"keyword": artist})])
        add("multi_completion", f"查{city}今天天气，同时查看我的提醒", [goal("weather", "maps_weather", {"city": city}), goal("planning", "list_reminders")])
        add("multi_completion", f"查{city}今天天气，同时搜索{artist}歌曲，并查看我的行程清单", [goal("weather", "maps_weather", {"city": city}), goal("music", "search_music", {"keyword": artist}), goal("planning", "list_itineraries")])
        title = f"{city}天气记录"
        add("multi_completion", f"查{city}今天天气，然后把查到的完整天气结果保存到行程，标题为{title}",
            [goal("weather", "maps_weather", {"city": city}), goal("planning", "save_itinerary", {"title": title}, receipt_from="maps_weather")])
    for category, label in (("restaurant", "饭店"), ("ktv", "KTV"), ("cinema", "电影院"), ("cafe", "咖啡店")):
        add("multi_completion", f"找无锡站附近的{label}，同时查无锡今天的天气", [goal("local_life", "maps_local_search", {"category": category, "anchor": "无锡站"}, detail=True), goal("weather", "maps_weather", {"city": "无锡"})])
    for artist in ("周杰伦", "陶喆", "陈奕迅"):
        add("multi_completion", f"从无锡站导航到东南大学无锡国际校区，同时搜索{artist}歌曲",
            [goal("navigation", "maps_route", origin_contains="无锡站"), goal("music", "search_music", {"keyword": artist})])
    for city in ("北京", "成都", "深圳"):
        add("multi_completion", f"查{city}今天的天气，同时查看我的提醒和行程清单", [goal("weather", "maps_weather", {"city": city}), goal("planning", "list_reminders"), goal("planning", "list_itineraries")])

    for city in ("无锡", "杭州", "南京", "上海", "苏州"):
        for day in ("今天", "明天", "后天"):
            add("clarified_completion", f"查询{day}天气", [goal("weather", "maps_weather", {"city": city, "forecast_date": {"今天": "$today", "明天": "$tomorrow", "后天": "$day_after"}[day]})], answers={"city": city}, require_fields=["city"])
    for anchor in ("无锡站", "杭州东站"):
        for category, label in (("restaurant", "饭店"), ("ktv", "KTV"), ("cinema", "电影院"), ("cafe", "咖啡店")):
            add("clarified_completion", f"帮我找附近的{label}", [goal("local_life", "maps_local_search", {"category": category, "anchor": anchor}, detail=True)], answers={"anchor": anchor}, require_fields=["anchor"])
    for destination, origin in (("东南大学无锡国际校区", "无锡站"), ("南京南站", "南京站"), ("杭州西湖", "杭州东站"), ("上海外滩", "上海站"), ("拙政园", "苏州站")):
        add("clarified_completion", f"导航到{destination}", [goal("navigation", "maps_route", origin_contains=origin)], answers={"origin": origin}, require_fields=["origin"])
    add("clarified_completion", "找无锡站附近的饭店", [goal("local_life", "maps_local_search", {"category": "restaurant", "anchor": "无锡站"}, detail=True), goal("music", "search_music", {"keyword": "周杰伦"})], compound="同时搜索周杰伦的歌曲", require_compound=True)
    add("clarified_completion", "找杭州东站附近的咖啡店", [goal("local_life", "maps_local_search", {"category": "cafe", "anchor": "杭州东站"}, detail=True), goal("weather", "maps_weather", {"city": "杭州"})], compound="同时查杭州今天的天气", require_compound=True)
    counts = Counter(case["group"] for case in cases)
    if set(counts.values()) != {30} or len(counts) != 3:
        raise ValueError(counts)
    return cases


def main():
    path = Path("evaluations/data/completion-v1.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    compounds = [
        ("查今天天气", [goal("weather", "maps_weather", {"city": "无锡"}), goal("music", "search_music", {"keyword": "周杰伦"})], {"city": "无锡"}, "city", "同时搜索周杰伦的歌曲"),
        ("查今天天气", [goal("weather", "maps_weather", {"city": "杭州"}), goal("music", "search_music", {"keyword": "陈奕迅"})], {"city": "杭州"}, "city", "顺便搜索陈奕迅歌曲"),
        ("找无锡站附近的饭店", [goal("local_life", "maps_local_search", {"category": "restaurant", "anchor": "无锡站"}, detail=True), goal("music", "search_music", {"keyword": "陶喆"})], {}, None, "同时搜索陶喆歌曲"),
        ("搜索陈奕迅的歌曲", [goal("music", "search_music", {"keyword": "陈奕迅"}), goal("weather", "maps_weather", {"city": "南京"})], {}, None, "另外查南京今天的天气"),
        ("导航到东南大学无锡国际校区", [goal("navigation", "maps_route", origin_contains="无锡站"), goal("music", "search_music", {"keyword": "孙燕姿"})], {"origin": "无锡站"}, "origin", "同时搜索孙燕姿歌曲"),
        ("导航到杭州西湖", [goal("navigation", "maps_route", origin_contains="杭州东站"), goal("weather", "maps_weather", {"city": "杭州"})], {"origin": "杭州东站"}, "origin", "并且查杭州今天的天气"),
    ]
    rows = [{"id": f"compound_completion-{index:02d}", "group": "compound_completion", "query": query, "goals": goals,
             "user_policy": {"answers": answers, "require_fields": [field] if field else [], "compound_on_field": field,
                             "compound": command, "compound_separator": "", "require_compound": True},
             "source": "authored_compound_regression_v1"}
            for index, (query, goals, answers, field, command) in enumerate(compounds, 1)]
    (path.parent / "completion-compound-v1.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(json.dumps({"path": str(path), "count": len(cases), "groups": dict(Counter(case["group"] for case in cases))}))


if __name__ == "__main__":
    main()
