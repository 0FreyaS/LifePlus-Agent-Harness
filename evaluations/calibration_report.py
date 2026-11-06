"""Export portable, redacted evidence from isolated calibration runs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import types

from .run import percentile, score_plan

ROOT = Path(__file__).resolve().parents[1]


def sanitize(value):
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items() if key not in {
            "geometry", "map_geometry", "polyline", "photos", "phone", "api_key", "authorization"}}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?:xai-|apikey_)[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
    return value


def meter(events):
    attempts = {}
    for event in events:
        if event.get("type", "").startswith("model.attempt.") and event.get("span_id"):
            attempts[event["span_id"]] = event
    known, missing = 0, 0
    for event in attempts.values():
        usage = event.get("usage") or {}
        total = usage.get("total_tokens")
        if type(total) is not int:
            total = (usage["input_tokens"] + usage["output_tokens"]
                     if all(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens")) else None)
        if total is None:
            missing += 1
        else:
            known += total
    return {"model_attempts": len(attempts), "known_total_tokens": known,
            "attempts_missing_usage": missing, "total_tokens": None if missing else known}


def archive_run(source, destination):
    source = source.resolve()
    target = destination / source.name
    if target.exists():
        raise ValueError("Evidence archive already exists")
    target.mkdir(parents=True)
    summary = json.loads((source / "summary.json").read_text())
    rows = [json.loads(line) for line in (source / "results.jsonl").read_text().splitlines()]
    manifest = json.loads((source / "manifest.json").read_text())
    dataset = Path(manifest["dataset"]) if "dataset" in manifest else None
    if dataset is None:
        dataset = next((path for path in (ROOT / "evaluations/data").glob("calibrated*/*.jsonl")
                        if hashlib.sha256(path.read_bytes()).hexdigest() == manifest.get("dataset_sha256")), None)
    if (source / "cases.json").exists():
        cases = json.loads((source / "cases.json").read_text())
    elif dataset and dataset.is_file():
        cases = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    else:
        cases = []
    selected = {row["id"] for row in rows}
    (target / "cases.jsonl").write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases if case["id"] in selected))
    db_path = source / ("runtime.sqlite3" if (source / "runtime.sqlite3").exists() else "audit.sqlite3")
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) if db_path.is_file() else None
    try:
        for row in rows:
            if row.get("audit_id") and db:
                event = db.execute("SELECT data_json FROM input_event_spans WHERE input_event_id=? AND type='input.events.proposed'", (row["audit_id"],)).fetchone()
                if event:
                    row["proposal"] = json.loads(event[0])["decision"]
            if row.get("owner") and db:
                events = [{"type": kind, **json.loads(data)} for kind, data in db.execute(
                    "SELECT e.type,e.data_json FROM events e JOIN tasks t ON e.task_id=t.id WHERE t.user_id=? ORDER BY e.created_at,e.seq", (row["owner"],))]
                events += [{"type": kind, **json.loads(data)} for kind, data in db.execute(
                    "SELECT s.type,s.data_json FROM input_event_spans s JOIN input_event_decisions d ON s.input_event_id=d.id WHERE d.user_id=? ORDER BY s.seq", (row["owner"],))]
                row["usage"] = meter(events)
                row["trace_file"] = row["id"] + ".trace.json"
                (target / row["trace_file"]).write_text(json.dumps(sanitize(events), ensure_ascii=False))
                row.pop("trace_files", None)
    finally:
        if db:
            db.close()
    event_run = "grade" in rows[0]
    component_run = "passed" in rows[0]
    passed = sum(row["grade"]["passed"] if event_run else row["passed"] if component_run else row["completed"] for row in rows)
    totals = [row.get("usage", {}) for row in rows]
    tokens = sum(usage.get("known_total_tokens", 0) for usage in totals)
    unknown = sum(usage.get("attempts_missing_usage", usage.get("responses_missing_usage", 0)) for usage in totals)
    summary["portable_usage"] = {"known_total_tokens": tokens, "attempts_missing_usage": unknown,
                                  "total_tokens": None if unknown else tokens}
    (target / "manifest.json").write_text(json.dumps(sanitize(manifest), ensure_ascii=False, indent=2))
    (target / "summary.json").write_text(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    (target / "results.jsonl").write_text("".join(json.dumps(sanitize(row), ensure_ascii=False) + "\n" for row in rows))
    return {"run": source.name, "scope": "事件理解" if event_run else "真实模型组件" if component_run else "真实MCP端到端", "passed": passed, "tested": len(rows),
            "p50_ms": percentile([row["elapsed_ms"] for row in rows], .5), "p95_ms": percentile([row["elapsed_ms"] for row in rows], .95),
            "known_total_tokens": tokens, "attempts_missing_usage": unknown}


def replay(destination, events_run, plans_run, baseline="eab3c41"):
    from car_agent.freshness import utcnow
    from car_agent.input_events import grounded_decision, InputDecision
    from .input_event_cases import make_waiting, grade

    modules, sources = {}, {}
    for name in ("user_events", "input_events"):
        source = subprocess.check_output(["git", "show", f"{baseline}:src/car_agent/{name}.py"], cwd=ROOT).decode()
        sources[name] = hashlib.sha256(source.encode()).hexdigest()
        module = types.ModuleType("car_agent._baseline_" + name)
        module.__package__ = "car_agent"
        sys.modules[module.__name__] = module
        exec(compile(source, f"{baseline}/{name}.py", "exec"), module.__dict__)
        modules[name] = module
    for name in ("ORDINAL_REPLY", "selection_index", "split_resume_message"):
        setattr(modules["input_events"], name, getattr(modules["user_events"], name))
    data = destination / events_run
    cases = {case["id"]: case for case in map(json.loads, (data / "cases.jsonl").read_text().splitlines())}
    observations = []
    for row in map(json.loads, (data / "results.jsonl").read_text().splitlines()):
        if not row.get("proposal"):
            observations.append({"id": row["id"], "before": False, "after": False, "reason": "no_valid_model_proposal"})
            continue
        case = cases[row["id"]]
        waiting = make_waiting(case, utcnow())
        before = modules["input_events"].grounded_decision(modules["input_events"].InputDecision.model_validate(row["proposal"]), case["query"], waiting, case.get("focus"))
        after = grounded_decision(InputDecision.model_validate(row["proposal"]), case["query"], waiting, case.get("focus"))
        observations.append({"id": row["id"], "before": grade(case, before)["passed"], "after": grade(case, after)["passed"]})
    report = {"scope": "same_real_proposals_and_corrected_state_host_replay_not_model_or_e2e_AB", "baseline": baseline,
              "baseline_sources": sources, "tested": len(observations), "before": sum(r["before"] for r in observations),
              "after": sum(r["after"] for r in observations), "rows": observations}
    (destination / "host-replay.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    data = destination / plans_run
    gold = {case["id"]: case for case in map(json.loads, (ROOT / "evaluations/data/calibrated-20261106-v4/components.jsonl").read_text().splitlines())}
    rows = []
    for row in map(json.loads, (data / "results.jsonl").read_text().splitlines()):
        passed = score_plan(gold[row["id"]], row.get("prediction", {}).get("tasks", []))[0] if row.get("prediction") else False
        rows.append({"id": row["id"], "original": row["passed"], "rescored": passed})
    score_report = {"scope": "same_recorded_plans_corrected_scoring_not_new_inference", "tested": len(rows),
                    "before": sum(r["original"] for r in rows), "after": sum(r["rescored"] for r in rows), "rows": rows}
    (destination / "plan-score-replay.json").write_text(json.dumps(score_report, ensure_ascii=False, indent=2))
    return report, score_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-events")
    parser.add_argument("--replay-plans")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = [archive_run(source, args.output) for source in args.run]
    (args.output / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    lines = ["# 2026-11-06 评测校准与修复证据", "", "各轮是迭代回归，提示词、夹具或Gold存在版本变化，不是随机A/B；首轮失败保留。",
             "本目录不包含API Key、SQLite数据库、照片和大尺寸地图几何。已保留逐条目标、模型提案、宿主结果及执行证据。", "",
             "| Run | 范围 | 通过/提交 | P50(ms) | P95(ms) | 已知Token | 缺用量attempt |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['run']} | {row['scope']} | {row['passed']}/{row['tested']} | {row['p50_ms']} | {row['p95_ms']} | {row['known_total_tokens']} | {row['attempts_missing_usage']} |")
    lines += ["", "事件仅测语义解释与宿主绑定，不执行工具。端到端为真实模型、ASGI Handler、Graph、MCP和隔离SQLite，自动回复不计真实人工等待；未验证公网或浏览器时延。",
              "有setup_queries的样本耗时包含同会话前置任务，同时保留setup_elapsed_ms。Token来自全部任务事件和输入事件attempt去重汇总，缺失为未知。",
              "370条组件和290条端到端是校准数据规模，不能写成已全部通过。事件完整100条与针对性12会话端到端分别报告。"]
    if args.replay_events and args.replay_plans:
        host, score = replay(args.output, args.replay_events, args.replay_plans)
        lines += ["", f"同一批真实模型提案的宿主回放：{host['before']}/{host['tested']} → {host['after']}/{host['tested']}，只验证宿主校验变化，不是模型准确率或端到端A/B。",
                  f"同一批规划输出评分重算：{score['before']}/{score['tested']} → {score['after']}/{score['tested']}，改善来自none/等价计划评分修正，不是模型能力提升。"]
    (args.output / "README.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    main()
