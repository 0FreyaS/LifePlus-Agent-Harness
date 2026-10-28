"""Authored event-routing cases; business execution is evaluated separately."""

MUSIC = {"id": "music", "domain": "music", "query": "搜索周杰伦歌曲", "kind": "selection", "candidates": ["晴天", "七里香"]}
PLACE = {"id": "place", "domain": "navigation", "query": "导航到东南大学", "kind": "selection", "candidates": ["南京校区", "无锡校区"]}
CITY = {"id": "weather", "domain": "weather", "query": "查询明天天气", "kind": "clarification", "field": "city"}
ORIGIN = {"id": "route", "domain": "navigation", "query": "导航到西湖", "kind": "clarification", "field": "origin"}
APPROVAL = {"id": "save", "domain": "planning", "query": "保存行程", "kind": "approval"}


def case(id, query, waiting, route, target=None, index=None, new_terms=(), answer_terms=(), focus=None):
    return {"id": id, "query": query, "waiting": waiting, "focus": focus,
            "expected": {"route": route, "target": target, "index": index,
                         "new_terms": list(new_terms), "answer_terms": list(answer_terms)},
            "source": "authored_input_event_v1"}


CASES = [
    case("rule01", "第二个", [MUSIC], "resume", "music", 2),
    case("rule02", "第二首", [MUSIC], "resume", "music", 2),
    case("rule03", "七里香", [MUSIC], "resume", "music", 2),
    case("rule04", "无锡", [CITY], "resume", "weather", answer_terms=["无锡"]),
    case("rule05", "第二个，同时查杭州天气", [MUSIC], "resume", "music", 2, ["杭州", "天气"]),
    case("rule06", "无锡，顺便搜索陶喆歌曲", [CITY], "resume", "weather", new_terms=["陶喆", "歌曲"], answer_terms=["无锡"]),
    case("rule07", "查杭州天气", [MUSIC], "new", new_terms=["杭州", "天气"]),
    case("rule08", "取消任务", [MUSIC], "cancel", "music"),
    case("rule09", "第二个", [MUSIC, PLACE], "clarify"),
    case("rule10", "第四首", [MUSIC], "clarify"),
    case("llm01", "第二个吧，杭州那边的天气也帮我看看", [MUSIC], "resume", "music", 2, ["杭州", "天气"]),
    case("llm02", "我想要七里香那首，再看看杭州那边天气", [MUSIC], "resume", "music", 2, ["杭州", "天气"]),
    case("llm03", "歌曲选第二首", [MUSIC, PLACE], "resume", "music", 2),
    case("llm04", "地点我选第二个", [MUSIC, PLACE], "resume", "place", 2),
    case("llm05", "无锡校区，杭州那边天气也看看", [PLACE], "resume", "place", 2, ["杭州", "天气"]),
    case("llm06", "我就在无锡，周杰伦的歌曲也帮我找找", [CITY], "resume", "weather", new_terms=["周杰伦", "歌曲"], answer_terms=["无锡"]),
    case("llm07", "起点是杭州东站，另外也看看杭州天气", [ORIGIN], "resume", "route", new_terms=["杭州", "天气"], answer_terms=["杭州东站"]),
    case("llm08", "好的", [APPROVAL], "clarify"),
    case("llm09", "可以的，直接帮我保存吧", [APPROVAL], "clarify"),
    case("llm10", "我觉得第二个挺合适", [MUSIC, PLACE], "clarify"),
    case("llm11", "第二个先别选，杭州天气也先别查", [MUSIC], "clarify"),
    case("llm12", "“第二个”这句话是什么意思", [MUSIC], "new", new_terms=["是什么意思"]),
    case("llm13", "我说的不是刚才那个", [MUSIC, PLACE], "clarify"),
    case("llm14", "音乐选第二个，地点也选第二个", [MUSIC, PLACE], "clarify"),
    case("llm15", "聊聊杭州有什么好玩的", [MUSIC], "new", new_terms=["杭州", "好玩的"]),
    case("llm16", "想查的城市是无锡", [CITY], "resume", "weather", answer_terms=["无锡"]),
    case("llm17", "随便", [MUSIC, PLACE], "clarify"),
    case("focus01", "第二个吧，杭州那边天气也看看", [MUSIC, PLACE], "resume", "music", 2, ["杭州", "天气"], focus="music"),
    case("focus02", "地点选第二个", [MUSIC, PLACE], "clarify", focus="music"),
    case("no_pending", "你好，聊聊今天的计划", [], "new", new_terms=["今天", "计划"]),
]

COMPLETION_CASES = [
    {"id": "event_completion-01", "group": "event_completion", "query": "搜索周杰伦歌曲",
     "goals": [{"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}},
               {"domain": "weather", "tool": "maps_weather", "arguments": {"city": "杭州", "forecast_date": "$today"}}],
     "user_policy": {"compound": "杭州那边今天的天气也帮我看看", "compound_separator": "，", "require_compound": True},
     "source": "authored_input_event_completion_v1"},
    {"id": "event_completion-02", "group": "event_completion", "query": "查今天天气",
     "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$today"}},
               {"domain": "music", "tool": "search_music", "arguments": {"keyword": "陶喆"}}],
     "user_policy": {"answers": {"city": "无锡"}, "require_fields": ["city"], "compound_on_field": "city",
                     "compound": "陶喆的歌曲也帮我找找", "compound_separator": "，", "require_compound": True},
     "source": "authored_input_event_completion_v1"},
]

HOLDOUT_CASES = [
    case("holdout01", "就要七里香吧，南京今天的天气也查一眼", [MUSIC], "resume", "music", 2, ["南京", "天气"]),
    case("holdout02", "无锡校区，顺便看看南京天气", [PLACE], "resume", "place", 2, ["南京", "天气"]),
    case("holdout03", "提醒列表的第二条", [MUSIC, PLACE], "clarify"),
    case("holdout04", "歌曲第二首，南京今天的天气也看看", [MUSIC, PLACE], "resume", "music", 2, ["南京", "天气"], focus="music"),
    case("holdout05", "七里香，南京天气也看看", [MUSIC], "resume", "music", 2, ["南京", "天气"]),
    case("holdout06", "第一个先别选了", [MUSIC], "clarify"),
]


def make_waiting(case, now):
    import json
    from datetime import timedelta
    return [{"id": t["id"], "query": t["query"], "domain": t["domain"], "status": "waiting_user",
             "interrupt_json": json.dumps({"id": "i-" + t["id"], "kind": t["kind"], "field": t.get("field"),
                 "expires_at": (now + timedelta(minutes=5)).isoformat(),
                 "candidates": [{"name": name} for name in t.get("candidates", [])]}, ensure_ascii=False)} for t in case["waiting"]]


def grade(case, decision):
    from car_agent.input_events import exact_selection
    expected, errors = case["expected"], []
    if decision.route != expected["route"]:
        errors.append("route")
    if decision.task_id != expected["target"]:
        errors.append("target")
    if expected["index"] is not None:
        target = next(t for t in case["waiting"] if t["id"] == expected["target"])
        if exact_selection(decision.answer or "", [{"name": name} for name in target["candidates"]]) != expected["index"]:
            errors.append("selection")
    if bool(decision.new_query) != bool(expected["new_terms"]):
        errors.append("new_task_presence")
    if any(term not in (decision.new_query or "") for term in expected["new_terms"]):
        errors.append("new_task_content")
    if any(term not in (decision.answer or "") for term in expected["answer_terms"]):
        errors.append("answer_content")
    return {"passed": not errors, "errors": errors}
