"""Frozen v1 pure reply / compound event experiment; authored before real calls."""
import argparse
import json
from pathlib import Path

from .input_event_cases import MUSIC, PLACE, CITY, APPROVAL, case


def make(id, query, waiting, group, route="resume", target=None, index=None, new=(), answer=(), **extra):
    value = case(id, query, waiting, route, target, index, new, answer)
    value.update(group=group, expected_filter="resume_only" if group == "pure_reply" else "needs_llm",
        source="authored_resume_filter_v1", **extra)
    return value


CASES = [
    make("pure01", "无锡校区吧", [PLACE], "pure_reply", target="place", index=2),
    make("pure02", "南京校区吧", [PLACE], "pure_reply", target="place", index=1),
    make("pure03", "第二个吧", [PLACE], "pure_reply", target="place", index=2),
    make("pure04", "第一个", [PLACE], "pure_reply", target="place", index=1),
    make("pure05", "第二首", [MUSIC], "pure_reply", target="music", index=2),
    make("pure06", "我选七里香", [MUSIC], "pure_reply", target="music", index=2),
    make("pure07", "晴天吧", [MUSIC], "pure_reply", target="music", index=1),
    make("pure08", "选南京校区", [PLACE], "pure_reply", target="place", index=1),
    make("pure09", "无锡", [CITY], "pure_reply", target="weather", answer=["无锡"]),
    make("pure10", "南京", [CITY], "pure_reply", target="weather", answer=["南京"]),
    make("pure11", "无锡吧", [CITY], "pure_reply", target="weather", answer=["无锡"]),
    make("pure12", "歌曲第二首", [MUSIC, PLACE], "pure_reply", target="music", index=2),
    make("compound01", "无锡校区吧，顺便查下明天无锡天气", [PLACE], "compound", target="place", index=2, new=["明天", "无锡", "天气"]),
    make("compound02", "第二个，你再帮我查无锡天气", [PLACE], "compound", target="place", index=2, new=["无锡", "天气"]),
    make("compound03", "第一个，同时搜索陶喆的歌曲", [PLACE], "compound", target="place", index=1, new=["陶喆", "歌曲"]),
    make("compound04", "七里香，再看看无锡明天天气", [MUSIC], "compound", target="music", index=2, new=["无锡", "明天", "天气"]),
    make("compound05", "无锡，顺便搜索周杰伦歌曲", [CITY], "compound", target="weather", new=["周杰伦", "歌曲"], answer=["无锡"]),
    make("compound06", "南京校区，帮我查南京明天天气", [PLACE], "compound", target="place", index=1, new=["南京", "明天", "天气"]),
    make("compound07", "先查无锡天气，歌曲第二首", [MUSIC], "compound", target="music", index=2, new=["无锡", "天气"]),
    make("compound08", "第二首，杭州天气也看看，不要搜索别的歌", [MUSIC], "compound", target="music", index=2, new=["杭州", "天气", "不要搜索别的歌"]),
    make("compound09", "无锡校区吧，另外帮我查无锡天气并搜索周杰伦歌曲", [PLACE], "compound", target="place", index=2, new=["无锡", "天气", "周杰伦", "歌曲"]),
    make("compound10", "第二个，接着搜索周杰伦的歌", [PLACE], "compound", target="place", index=2, new=["周杰伦", "歌"]),
    make("compound11", "我选晴天，南京那边明天会下雨吗", [MUSIC], "compound", target="music", index=1, new=["南京", "明天", "下雨"]),
    make("compound12", "第二首，同时列出我的行程", [MUSIC], "compound", target="music", index=2, new=["列出", "行程"]),
    make("boundary01", "第二个", [MUSIC, PLACE], "boundary", "clarify"),
    make("boundary02", "无锡校区吧", [PLACE, {**PLACE, "id": "place2"}], "boundary", "clarify"),
    make("boundary03", "第二个", [PLACE], "boundary", "clarify", expired=True),
    make("boundary04", "取消任务", [MUSIC], "boundary", "cancel", target="music"),
    make("boundary05", "第一个先别选了", [MUSIC], "boundary", "clarify"),
    make("boundary06", "“第二个”是什么意思", [MUSIC], "boundary", "new", new=["是什么意思"]),
    make("boundary07", "好的", [APPROVAL], "boundary", "clarify"),
    make("boundary08", "好的，顺便查无锡天气", [APPROVAL], "boundary", "clarify"),
    make("boundary09", "先选歌曲第二首，再选校区第二个", [MUSIC, PLACE], "boundary", "clarify"),
    make("boundary10", "查无锡天气", [MUSIC], "boundary", "new", new=["无锡", "天气"]),
    make("boundary11", "你好", [], "boundary", "new", new=["你好"]),
    make("boundary12", "第四个", [PLACE], "boundary", "clarify"),
]

E2E = [
    {"id": "resume-pure-music", "group": "pure_reply", "query": "搜索周杰伦歌曲",
     "goals": [{"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}}],
     "user_policy": {"selection_text": True, "reply_entry": "composer", "require_fields": []}},
    {"id": "resume-pure-city", "group": "pure_reply", "query": "查明天天气",
     "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$tomorrow"}}],
     "user_policy": {"answers": {"city": "无锡"}, "require_fields": ["city"], "reply_entry": "composer"}},
    {"id": "resume-compound-music", "group": "compound", "query": "搜索周杰伦歌曲",
     "goals": [{"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}},
               {"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$tomorrow"}}],
     "user_policy": {"compound": "你再帮我查明天无锡天气", "require_compound": True, "reply_entry": "composer"}},
    {"id": "resume-compound-city", "group": "compound", "query": "查明天天气",
     "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$tomorrow"}},
               {"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}}],
     "user_policy": {"answers": {"city": "无锡"}, "require_fields": ["city"], "compound_on_field": "city",
                     "compound": "顺便搜索周杰伦歌曲", "require_compound": True, "reply_entry": "composer"}},
    {"id": "resume-card-control", "group": "structured_card", "query": "搜索周杰伦歌曲",
     "goals": [{"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}}], "user_policy": {}},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for name, values in (("events.jsonl", CASES), ("end_to_end.jsonl", E2E)):
        (args.output / name).write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values))


if __name__ == "__main__":
    main()
