"""Derive reviewable datasets without changing historical cases or observations."""
from __future__ import annotations

import argparse
from copy import deepcopy
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from car_agent.config import DEFAULT_CURRENT_LOCATION
from car_agent.harness import explicit_route_endpoints
from car_agent.travel_modes import explicit_travel_mode
from evaluations.dataset import build_cases

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "测试回报/测试数据集"
VERSION = "calibrated-20261106-v4"
FROZEN_SOURCES = ROOT / "evaluations/data/calibrated-20261106-v4/sources"


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def date_alias(query):
    return "$day_after" if "后天" in query else "$tomorrow" if "明天" in query else "$today"


def calibrate_event(original):
    case = deepcopy(original)
    suffix = case["id"]
    expected = case["expected"]
    if suffix == "event-044":
        expected["index"] = 2
    if suffix == "event-051":
        expected["index"] = 1
    if suffix in {"event-066", "event-068", "event-069", "event-071", "event-072", "event-075", "event-090", "event-093", "event-094", "event-096", "event-098", "event-099"}:
        expected.update(route="clarify", target=None, index=None, new_terms=[], answer_terms=[])
    if suffix == "event-064":
        expected["new_terms"] = ["路线", "保存"]
    if suffix == "event-077":
        case["query"] = "“第二个”这个词是什么意思"
    if suffix == "event-097":
        expected["new_terms"] = ["解释", "第二个"]
    return case


def calibrate_component(original):
    case = deepcopy(original)
    query, expected = case["query"], case["expected"]
    slots = expected.get("slots", {})
    if "forecast_date" in slots:
        slots["forecast_date"] = date_alias(query)
    if expected.get("action") is None:
        expected.pop("action", None)
    if case["group"] == "slot_extraction":
        expected["slot_stage"] = "extracted"
        for name in ("min_rating", "max_cost"):
            if name in slots and not re.search(r"\d", query):
                slots.pop(name)
        if slots.get("city") and slots["city"].removesuffix("市") not in query:
            slots.pop("city")
        if case["id"] == "slot_supplement-024":
            slots["destination"] = "会展中心"
        if "刚才" in query and "路线" in query:
            slots.pop("title", None)
            slots["details"] = None
        if case["id"] == "slot_supplement-068":
            case["query"] = "提醒我明天下午两点开会"
            slots["remind_at"] = "$tomorrow14"
    if case["group"] == "clarification":
        field = expected.get("missing_field")
        if field in {"origin", "anchor"}:
            expected["missing_field"] = "destination" if query in {"帮我规划一条路线"} else None
        if query == "创建一个提醒":
            expected["acceptable_missing_fields"] = ["title", "remind_at"]
    if case["group"] == "multi_task" and query == "查上海和无锡天气":
        expected["goals"] = [{"domain": "weather", "query_contains": [city]} for city in ("上海", "无锡")]
    # Search/selection/detail are an allowed single domain workflow, not two autonomous goals.
    if case["group"] == "multi_task" and case["id"] in {"plan-094", "plan-095"}:
        equivalent = deepcopy(expected)
        expected.update(domains=["local_life"], dependency="independent")
        expected["goals"] = [{"domain": "local_life", "query_contains": ["详情", "第一" if case["id"] == "plan-094" else "第二"]}]
        expected["equivalent_plans"] = [equivalent]
    return case


def calibrate_completion(original):
    case = deepcopy(original)
    if case["id"] in {f"clarified_completion-{n}" for n in range(91, 101)}:
        return None, "复合请求目标领域及用户回复策略错配，需重新人工标注，不计入新验收分母"
    query, policy = case["query"], case.setdefault("user_policy", {})
    policy["require_fields"] = [field for field in policy.get("require_fields", []) if field not in {"origin", "anchor"}]
    policy["forbid_clarification_fields"] = ["origin", "anchor"]
    answers = policy.setdefault("answers", {})
    answers.pop("origin", None)
    answers.pop("anchor", None)
    for goal in case["goals"]:
        arguments = goal.setdefault("arguments", {})
        if goal.get("tool") == "maps_weather":
            arguments["forecast_date"] = date_alias(query)
        if goal.get("tool") == "maps_route":
            goal["evidence_contract"] = "v2"
            arguments["travel_mode"] = explicit_travel_mode(query) or "driving"
            if arguments.get("city") and arguments["city"].removesuffix("市") not in query:
                arguments.pop("city")
            endpoints = explicit_route_endpoints(query)
            if not endpoints:
                arguments.pop("origin", None)
                goal["origin_contains"] = DEFAULT_CURRENT_LOCATION
            goal["destination_aliases"] = [arguments["destination"]] if arguments.get("destination") else []
        if goal.get("tool") == "maps_local_search":
            for name in ("min_rating", "max_cost"):
                if name in arguments and not re.search(r"\d", query):
                    arguments.pop(name)
            anchor = arguments.get("anchor", "")
            if not anchor or anchor not in query:
                arguments["anchor"] = DEFAULT_CURRENT_LOCATION
                arguments["city"] = "无锡"
        if goal.get("tool") == "save_itinerary":
            if "标题" not in query:
                arguments.pop("title", None)
                answers.setdefault("title", "测试行程")
            if "刚才" in query and "路线" in query:
                case["setup_queries"] = ["从无锡站驾车到东南大学无锡国际校区"]
                goal["receipt_from"] = "maps_route"
        if goal.get("tool") == "create_reminder":
            answers.setdefault("title", arguments.get("title", "测试提醒"))
    return case, None


def build(output):
    if output.exists():
        raise ValueError("Use a new version directory; frozen data must not be overwritten")
    output.mkdir(parents=True)
    revisions, exclusions, sources = [], [], []

    def read(relative):
        path = DATA / relative
        if not path.is_file():
            path = FROZEN_SOURCES / relative
        raw = path.read_bytes()
        sources.append({"file": "测试回报/测试数据集/" + relative, "sha256": hashlib.sha256(raw).hexdigest()})
        return load(path)

    event_cases = read("06-文本事件/文本事件36条.jsonl") + read("15-事件分类/事件分类补充64条-20261031-r1.jsonl")
    component_cases = [case for case in build_cases() if case["group"] in {"single_task", "multi_task", "slot_extraction", "clarification"}]
    for relative, group in (("17-单任务多任务判断/单任务多任务判断补充40条-20261031-r1.jsonl", "multi_task"),
                             ("18-槽位与工具参数提取/槽位与工具参数补充70条-20261031-r1.jsonl", "slot_extraction"),
                             ("16-信息澄清判断/信息澄清判断补充70条-20261031-r1.jsonl", "clarification")):
        component_cases.extend({**case, "group": group, "runner": "model"} for case in read(relative))
    completion_cases = []
    for name in ("单任务完成率30条-20261030-r2.jsonl", "多任务完成率30条-20261030-r2.jsonl", "信息澄清端到端30条-20261030-r2.jsonl",
                 "单任务补充70条-20261031-r1.jsonl", "多任务补充70条-20261031-r1.jsonl", "信息澄清端到端补充70条-20261031-r1.jsonl"):
        completion_cases.extend(read("14-端到端完成率/" + name))
    for original in completion_cases:
        if original["group"] == "single_completion" and original["id"].split("-")[-1].isdigit() and int(original["id"].split("-")[-1]) > 30:
            goal = original["goals"][0]
            action = {"maps_weather": "weather_lookup", "maps_route": "navigation_search", "search_music": "music_search", "maps_local_search": "local_search"}.get(goal.get("tool"), goal.get("tool"))
            component_cases.append({"id": original["id"], "group": "single_task", "runner": "model", "query": original["query"],
                                    "expected": {"domain": goal["domain"], "action": action}})
    for filename, inputs, transform in (("events.jsonl", event_cases, calibrate_event),
                                        ("components.jsonl", component_cases, calibrate_component),
                                        ("completion.jsonl", completion_cases, calibrate_completion)):
        cases = []
        for original in inputs:
            result = transform(original)
            case, reason = result if isinstance(result, tuple) else (result, None)
            if case is None:
                exclusions.append({"dataset": filename, "id": original["id"], "reason": reason, "original": original})
                continue
            if case != original:
                revisions.append({"dataset": filename, "id": case["id"], "before": original, "after": deepcopy(case)})
            case["calibration_version"] = VERSION
            cases.append(case)
        if len({case["id"] for case in cases}) != len(cases):
            raise ValueError("Duplicate IDs")
        (output / filename).write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases))
    manifest = {"version": VERSION, "sources": sources, "revisions": revisions, "excluded": exclusions,
                "scope": "derived authored cases; no new model results", "default_location": DEFAULT_CURRENT_LOCATION,
                "files": {path.name: {"count": len(load(path)), "groups": dict(Counter(case.get("group", "event") for case in load(path)))}
                          for path in output.glob("*.jsonl")}}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"files": manifest["files"], "excluded": len(exclusions), "revised": len(revisions)}


def snapshot_sources(output):
    manifest = json.loads((output / "manifest.json").read_text())
    for source in manifest["sources"]:
        path = ROOT / source["file"]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("Source data changed since calibration: " + source["file"])
        destination = output / "sources" / path.relative_to(DATA)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(raw)
    return {"source_snapshots": len(manifest["sources"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluations/data/calibrated-20261106-v4")
    parser.add_argument("--snapshot-sources", action="store_true")
    args = parser.parse_args()
    print(json.dumps(snapshot_sources(args.output) if args.snapshot_sources else build(args.output), ensure_ascii=False))
