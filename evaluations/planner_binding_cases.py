"""Prepare the seven failed planning regressions and 30 authored long-tail cases.

This module only writes frozen test inputs; it never calls a model or a tool.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from .planner_cases import case, task

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("测试回报/测试数据集/03-规划与依赖")
NAME = "规划绑定修复37条-v1"
DATASET = DIRECTORY / (NAME + ".jsonl")
PREPARATION = DIRECTORY / (NAME + ".准备记录.json")
SEED_IDS = {"dependency01", "dependency02", "dependency03", "dependency04", "dependency05", "boundary02", "boundary05"}


def weather(label, city):
    return task(label, "weather", "weather_lookup", query_terms=[city])


def route(label, origin, destination):
    return task(label, "navigation", "navigation_search", query_terms=[origin, destination])


def music(label, singer):
    return task(label, "music", "music_search", query_terms=[singer])


def local(label, anchor, category):
    return task(label, "local_life", "local_search", query_terms=[anchor, category])


def save(label, source, field="result", *, terms=()):
    return task(label, "planning", "save_itinerary", binds={"details": [source, field]}, query_terms=terms)


def long_tail():
    groups = {
        "weather_save": [
            ("帮我看看海口明天会不会下雨，查完把原始天气回执存进我的出行清单，别只存一句摘要。",
                [weather("w", "海口"), save("s", "w")], ["colloquial", "full_receipt"]),
            ("先查拉萨后天的天气；这次查到的整份结果麻烦帮我留一份行程记录。",
                [weather("w", "拉萨"), save("s", "w")], ["long_tail_city", "relative_date"]),
            ("杭州和宁波明天的天气都查一下，各自的结果分别存一份行程，不要混在一条里。",
                [weather("h", "杭州"), weather("n", "宁波"), save("hs", "h", terms=["杭州"]), save("ns", "n", terms=["宁波"])], ["same_domain", "two_sources"]),
            ("查一下厦门明天天气，把这份查询结果保存为行程《厦门出门参考》。",
                [weather("w", "厦门"), save("s", "w", terms=["厦门出门参考"])], ["explicit_title"]),
            ("苏州明天的天气帮我查查，查询回执原样记到行程里，别拿以前查过的结果代替。",
                [weather("w", "苏州"), save("s", "w")], ["fresh_evidence", "no_history_substitution"]),
            ("这次说的是南京：查明天天气，再保存本轮查到的完整结果到行程。",
                [weather("w", "南京"), save("s", "w")], ["explicit_override", "context_conflict"]),
        ],
        "route_save": [
            ("从无锡站骑车去东南大学无锡校区，路线出来后把完整方案存为行程《骑行上课》，不是启动导航。",
                [route("r", "无锡站", "东南大学无锡校区"), save("s", "r", "route", terms=["骑行上课"])], ["bicycling", "no_navigation_start"]),
            ("从杭州东站步行去西湖。等路线规划好，把这条路线收藏进我的行程；天气不用查。",
                [route("r", "杭州东站", "西湖"), save("s", "r", "route")], ["walking", "negated_weather"]),
            ("我说的保存，是保存从南京站到夫子庙的驾车路线结果，不是另建提醒。先把路线查出来。",
                [route("r", "南京站", "夫子庙"), save("s", "r", "route")], ["reversed_mention_order", "negated_reminder"]),
            ("分别规划无锡站到鼋头渚、杭州东站到西湖的步行路线，各自存一份行程，别把两个路线串起来。",
                [route("a", "无锡站", "鼋头渚"), route("b", "杭州东站", "西湖"), save("as", "a", "route", terms=["鼋头渚"]), save("bs", "b", "route", terms=["西湖"])], ["two_routes", "independent_branches"]),
            ("去东南大学的路线先规划一下，等我选好校区后，再把本次实际路线存到行程。",
                [task("r", "navigation", "navigation_search", query_terms=["东南大学"]), save("s", "r", "route")], ["missing_origin", "campus_ambiguity", "user_wait"]),
            ("从无锡站开车去太湖鼋头渚，保存的是规划得到的完整路线，不要只保存目的地名称。",
                [route("r", "无锡站", "鼋头渚"), save("s", "r", "route")], ["whole_route_not_name"]),
        ],
        "search_save": [
            ("把陶喆的歌搜出来，整份检索回执帮我放到行程里，先别播放。",
                [music("m", "陶喆"), save("s", "m")], ["negated_play", "full_receipt"]),
            ("想留周杰伦歌曲的搜索结果做备忘，麻烦先搜再保存到行程，叫《路上听》。",
                [music("m", "周杰伦"), save("s", "m", terms=["路上听"])], ["save_not_reminder", "explicit_title"]),
            ("周杰伦和陈奕迅的歌都搜一下，只把陈奕迅这次的搜索结果存入行程，周杰伦的不用保存。",
                [music("j", "周杰伦"), music("e", "陈奕迅"), save("s", "e", terms=["陈奕迅"])], ["same_domain", "source_disambiguation", "negative_scope"]),
            ("在无锡站附近找KTV，搜到的商家回执存到行程里；不用预订。",
                [local("l", "无锡站", "KTV"), save("s", "l")], ["local_life", "no_booking"]),
            ("先找南京新街口评分高的咖啡馆，然后保存这次完整检索回执，不是只存店名。",
                [local("l", "新街口", "咖啡"), save("s", "l")], ["local_filter", "whole_receipt_not_name"]),
            ("查杭州东站附近的电影院，把本轮检索回执存成行程《周五看电影》，不要创建提醒。",
                [local("l", "杭州东站", "电影院"), save("s", "l", terms=["周五看电影"])], ["explicit_title", "negated_reminder"]),
        ],
        "compound_binding": [
            ("查无锡明天天气，同时搜陶喆的歌；把天气结果存到行程，音乐结果只展示。",
                [weather("w", "无锡"), music("m", "陶喆"), save("s", "w")], ["parallel_plus_dependency", "selective_save"]),
            ("从杭州东站走路到西湖，另外查杭州天气、搜周杰伦歌曲，最后只保存那条实际路线到行程。",
                [route("r", "杭州东站", "西湖"), weather("w", "杭州"), music("m", "周杰伦"), save("s", "r", "route")], ["four_goals", "route_only_binding"]),
            ("找无锡站附近饭店，查无锡明天天气，并列出我的提醒；把饭店的检索回执保存到行程即可。",
                [local("l", "无锡站", "饭店"), weather("w", "无锡"), task("p", "planning", "list_reminders", query_terms=["提醒"]), save("s", "l")], ["four_goals", "same_domain_planning"]),
            ("查南京天气、搜索陶喆歌曲、找新街口附近咖啡店、查看我的提醒、查看我的行程，再把南京天气查询结果另存为一条行程。",
                [weather("w", "南京"), music("m", "陶喆"), local("l", "新街口", "咖啡"), task("p", "planning", "list_reminders", query_terms=["提醒"]), task("i", "planning", "list_itineraries", query_terms=["行程"]), save("s", "w")], ["six_goal_limit", "same_domain_atomic_goals"]),
            ("南京和无锡今天天气都查一下，只有南京的那份结果保存到行程，无锡的直接告诉我。",
                [weather("n", "南京"), weather("w", "无锡"), save("s", "n", terms=["南京"])], ["two_weather_sources", "no_last_result_leakage"]),
            ("搜索周杰伦歌曲，也查一下杭州天气；把歌曲的结果保存到行程，天气只展示。",
                [music("m", "周杰伦"), weather("w", "杭州"), save("s", "m")], ["source_not_last_task", "independent_roots"]),
        ],
        "binding_boundary": [
            ("查无锡天气，然后搜陶喆歌曲，两个结果直接告诉我，不要保存行程。",
                [weather("w", "无锡"), music("m", "陶喆")], ["then_not_dependency", "negated_write"]),
            ("规划无锡站到鼋头渚的路线；稍后我自己决定是否保存，当前先不要写行程。",
                [route("r", "无锡站", "鼋头渚")], ["conditional_not_authorized", "no_save_goal"]),
            ("查杭州今天的天气并保存本次回执到行程，不要拿上一轮南京天气的结果保存。",
                [weather("w", "杭州"), save("s", "w")], ["stale_context", "fresh_binding"]),
            ("从南京站到夫子庙的路线帮我查一下，保存行程就算了，只看路线，不用保存。",
                [route("r", "南京站", "夫子庙")], ["self_correction", "negated_save"]),
            ("查杭州明天天气后保存成行程《路线参考》，标题里虽然有路线，但我不是让你查路线。",
                [weather("w", "杭州"), save("s", "w", terms=["路线参考"])], ["quoted_title", "domain_keyword_distractor"]),
            ("给我搜索名叫《然后》的歌曲，把这次检索回执存成行程《不要忘记》。",
                [music("m", "然后"), save("s", "m", terms=["不要忘记"])], ["quoted_connector", "quoted_negative_word"]),
        ],
    }
    result = []
    for group, entries in groups.items():
        for index, (query, tasks, tags) in enumerate(entries, 1):
            brief = "历史城市是杭州，当前明确要求优先。" if "context_conflict" in tags else (
                "历史南京天气已经过期，不是本次执行回执。" if "stale_context" in tags else "")
            result.append({**case(f"binding-{group}-{index:02}", group, query, tasks, brief),
                "scope": "long_tail30", "source": "authored_binding_long_tail_v1", "tags": tags})
    return result


def dataset():
    source = ROOT / "测试回报/测试数据集/03-规划与依赖/规划与依赖30条.jsonl"
    seeds = [{**json.loads(line), "scope": "failed_seed7", "source": "frozen_D04_failed_regression",
              "tags": ["previous_failure", "data_dependency"]} for line in source.read_text().splitlines()
             if line.strip() and json.loads(line)["id"] in SEED_IDS]
    data = seeds + long_tail()
    if len(seeds) != 7 or len(data) != 37 or len({item["id"] for item in data}) != 37:
        raise ValueError("Expected exactly seven old failures and 30 new cases")
    return data


def preparation_record(data, raw):
    return {"status": "prepared_not_live_tested", "thinking": "disabled", "paid_calls": 0,
        "scopes": dict(Counter(item["scope"] for item in data)), "cases": len(data),
        "long_tail_groups": dict(Counter(item["group"] for item in data if item["scope"] == "long_tail30")),
        "binding_positive_cases": sum(any(spec["binds"] for spec in item["tasks"]) for item in data),
        "no_write_control_cases": sum(not any(spec["binds"] for spec in item["tasks"]) for item in data),
        "dataset": DATASET.as_posix(), "sha256": hashlib.sha256(raw).hexdigest(),
        "frozen_now": "2026-10-27T10:00:00+08:00"}


def prepare():
    data = dataset()
    raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in data).encode()
    path = ROOT / DATASET
    if path.exists() and path.read_bytes() != raw:
        raise ValueError("Frozen dataset changed; create a new version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    record = preparation_record(data, raw)
    (ROOT / PREPARATION).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    path.with_suffix(".说明.md").write_text(
        "# 规划绑定修复回归37条 v1\n\n原D04失败7条保持原输入/标签，新增30条人工编写长尾，五组各6条，"
        "覆盖天气保存、路线保存、音乐/商家检索保存、并行加数据依赖、否定/纠正边界。"
        "共34条需要结果绑定，3条禁止保存对照。不是37条线上日志、训练集或已完成的真实评测。\n\n"
        "scope区分failed_seed7和long_tail30，两个分母分别统计；旧场景复用不算新的独立样本。"
        "tasks含语义label、domain、binds、query_terms；action/slots是历史期望标签，不是全局模型输出字段。"
        "按grade_goals容许任务ID重命名、并行顺序交换，检查数量/领域/关键词/直接依赖/绑定；"
        "尚不覆盖所有自然语言约束的人工语义评分，也不测工具调用、业务写入及端到端完成率。\n\n"
        "固定日期2026-10-27T10:00:00+08:00；默认关闭思考，最多6目标，保存操作仍由领域层确认。"
        "本命令仅准备数据；真实模型通过率、P50/P95与Token以专用评测输出为准，不能根据数据冻结预填成功结果。\n")
    return record


if __name__ == "__main__":
    print(json.dumps(prepare(), ensure_ascii=False))
