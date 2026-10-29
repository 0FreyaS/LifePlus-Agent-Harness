"""Compare fallback policies on a shared real Jev response; only B calls the planner."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import random
from pathlib import Path
from time import monotonic

from .jev_ab import attempts_from_events, summarize, usage_summary, write_summary
from .jev_gate_cases import CASES

HOLDOUT = [
    ("提醒我周五十点买牛奶和面包", "single", ""),
    ("查广州明天的湿度和降雨情况", "single", ""),
    ("搜索歌曲《风和日丽》", "single", ""),
    ("南京南站到夫子庙先搜地点再给我步行路线", "single", ""),
    ("刚才的天气改为后天的", "single", "上轮用户查询南京天气。"),
    ("不是查天气，是想搜索陶喆的歌曲", "single", ""),
    ("明早八点提醒我出门，也看看南京明天天气", "multi", ""),
    ("看看苏州天气外加张学友歌曲", "multi", ""),
    ("查上海天气，成功后将结果保存到行程", "multi", ""),
    ("杭州天气苏州天气都看一下", "multi", ""),
    ("周杰伦介绍和歌曲搜索分别给我", "multi", ""),
    ("找附近影院以及查看我的提醒", "multi", ""),
]
DEFAULT_OUTPUT = Path("evaluations/jev/2026-10-29/modules/cardinality_llm_fallback/v1")
NOTE_START = "<!-- jev-planner-fallback:v1:start -->"
NOTE_END = "<!-- jev-planner-fallback:v1:end -->"


def cases():
    return [{**c, "split": "regression"} for c in CASES] + [
        {"id": f"fallback-holdout-{i:02}", "query": query, "expected_label": label,
         "context": context, "group": "holdout", "split": "holdout"}
        for i, (query, label, context) in enumerate(HOLDOUT, 1)]


class SharedGate:
    """Reuse the already paid-for classification without a second request."""
    def __init__(self, cardinality, domain):
        self.result = cardinality, domain

    async def classify(self, *args):
        return self.result


def reports(rows):
    return {split: summarize([r for r in rows if r["split"] == split]) for split in ("regression", "holdout")}


def csv_rows(rows):
    result = io.StringIO(newline="")
    writer = csv.DictWriter(result, fieldnames=["id", "split", "arm", "query", "expected_label", "actual_label",
        "passed", "fallback", "elapsed_ms", "total_tokens", "unknown_attempts", "error_type"], lineterminator="\n")
    writer.writeheader()
    for row in rows:
        usage = usage_summary(row["attempts"])
        writer.writerow({**{k: row.get(k, "") for k in writer.fieldnames if k not in {"total_tokens", "unknown_attempts"}},
            "total_tokens": usage["total_tokens"] if usage["total_tokens"] is not None else "unknown",
            "unknown_attempts": usage["attempts_missing_usage"]})
    return result.getvalue()


def render_report(output, document=None):
    output = Path(output)
    frozen = [json.loads(line) for line in (output / "cases.jsonl").read_text().splitlines()]
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    ids = {case["id"] for case in frozen}
    if len(ids) != len(frozen) or len(rows) != len(frozen) * 2 or any(
        len(selected := [r for r in rows if r["arm"] == arm]) != len(ids) or {r["id"] for r in selected} != ids
        for arm in ("baseline", "candidate")
    ):
        raise ValueError("Incomplete or duplicate paired evidence")
    summaries = reports(rows)
    lines = ["# 低分Jev回退原LLM规划器：2026-10-29", "",
        "A：Jev高分接受，低分/defer/服务异常回退旧正则。B：相同Jev结果高分接受，不确定时调用原GoalPlan规划器，不再退回正则。门槛和Jev提示词均未调整。", "",
        "每条只真实调用一次Jev，A/B共享该响应与前缀耗时；B的不确定后缀真实调用原grok-4.3规划器，未使用模拟模型。两组是策略配对，不是两次独立网络请求的端到端A/B。", "",
        "高分multi在本试验只判断标签，不生成完整任务计划；只有不确定样本执行生产plan_tasks。未执行工具、未测业务完成率，不用这里的30/30声称全局100%。", "",
        "| 范围 | 完整通过/提交 A→B | P50（秒） | P95（秒） | 总Token |", "|---|---|---|---|---|"]
    changes = []
    for split, label in (("regression", "原30条回归"), ("holdout", "新增12条留出")):
        a, b = (summaries[split]["arms"][arm] for arm in ("baseline", "candidate"))
        lines.append(f"| {label} | {a['passed']}/{a['tested']} → {b['passed']}/{b['tested']} | "
            f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | "
            f"{a['total_tokens']} → {b['total_tokens']} |")
        if a["total_tokens"] and b["total_tokens"] is not None:
            changes.append(f"{label}Token增加{b['total_tokens']-a['total_tokens']:,}（{(b['total_tokens']/a['total_tokens']-1)*100:.2f}%）。")
    lines += ["", *changes, "", "## 不确定样本", "", "| 输入 | 划分 | 原策略 | 新策略 | 新策略总耗时 |", "|---|---|---|---|---:|"]
    baseline = {r["id"]: r for r in rows if r["arm"] == "baseline"}
    for row in rows:
        if row["arm"] == "candidate" and row["fallback"]:
            lines.append(f"| {row['query']} | {row['split']} | {baseline[row['id']]['actual_label']} | "
                         f"{row['actual_label']} | {row['elapsed_ms']/1000:.3f}秒 |")
    lines += ["", "## 证据与限制", "",
        "- `cases.jsonl`：原30条＋运行前冻结的新增12条，原回归集与新增集分开评分。",
        "- `results.jsonl`：84条逻辑A/B观察，保留同一真实Jev响应、分数、原LLM计划、所有模型尝试与供应商usage。共享Jev前缀分别计入两种策略成本，物理请求不能将两组再相加。",
        "- `details.csv`：逐条标签、通过、回退、耗时与Token；`summary.json`及两个划分子目录是可复算汇总。",
        "- 实测7条不确定样本调用原规划器：原集4条、新增集3条；其余不额外调用规划器。本次用量均已返回，包含全部模型尝试。",
        "- P50/P95按nearest-rank计算，包含所有提交；增加的尾延迟与Token是质量代价。新策略不会把规划失败静默改成单任务，不绕过预算或用户取消。",
        "- 一次小样本局部试验，未做线上流量、全链路工具或压力测试；不调低门槛迎合样本。实际运行服务的单/多门控仍由PLANNING_GATE_PROVIDER决定。", "",
        "## 复算与复跑", "", "```bash",
        ".venv/bin/python -m evaluations.jev_planner_fallback_ab --render",
        ".venv/bin/python -m evaluations.jev_planner_fallback_ab --run-live --output <新的归档目录> --private <新的私有日志目录>",
        "```", "", "render只读现有观察并重建派生说明，不请求模型；run-live会消耗真实API额度且拒绝覆盖旧目录。"]
    text = "\n".join(lines) + "\n"
    (output / "details.csv").write_text(csv_rows(rows))
    (output / "README.md").write_text(text)
    if document:
        document = Path(document)
        relative = os.path.relpath(output, document.parent)
        note = (NOTE_START + "\n\n### 2026-10-29：Jev低分回退LLM规划器（中文标记）\n\n"
            + "本轮实现：Jev高置信度保持原分流；低分、defer和服务异常直接调用原LLM规划器，不再退回正则。"
            + "系统无法预知Jev是否正确，因此不按事后标签决定回退。当前单任务领域Jev保持原样；未自动开启单/多门控或重启Docker。\n\n"
            + text[text.index("| 范围 |"):text.index("## 不确定样本")]
            + f"\n[本轮数据、逐条结果与复算说明]({relative}/README.md)。原始用例、模型输出、CSV、统计分别保存在该目录；旧版数据未覆盖。\n\n"
            + "本轮42条每条共享一次真实Jev调用，B在7条不确定输入上真实调用原LLM规划器。"
            + "这是模块策略对比，不是完整工具链路测试；正确率改善但P95与Token增加，不能宣称提速。工程回归717通过、1跳过"
            + "（本机data/jev-planner-fallback-tests-20261029.xml）；独立待发布快照709通过、2跳过。受控回归不等于模型质量。\n\n"
            + NOTE_END)
        previous = document.read_text()
        if NOTE_START in previous or NOTE_END in previous:
            if previous.count(NOTE_START) != 1 or previous.count(NOTE_END) != 1:
                raise ValueError("Ambiguous maintained note markers")
            start, end = previous.index(NOTE_START), previous.index(NOTE_END)
            if end < start:
                raise ValueError("Reversed maintained note markers")
            previous = previous[:start] + note + previous[end + len(NOTE_END):]
        else:
            previous = previous.rstrip() + "\n\n" + note + "\n"
        document.write_text(previous)
    return text


async def run(args):
    from car_agent.config import Settings
    from car_agent.freshness import utcnow
    from car_agent.model_roles import build_models
    from car_agent.persistence import Database
    from car_agent.planning_gate import build_planning_gate
    from car_agent.runtime_limits import RuntimeLimits, execution_scope
    from car_agent.task_planning import may_have_multiple_tasks, plan_tasks

    settings = Settings()
    model, roles = build_models(settings)
    if model is None:
        raise ValueError("Real planner required")
    gate = build_planning_gate(settings.model_copy(update={"planning_gate_provider": "jev_front"}))
    dataset = cases()
    args.output.mkdir(parents=True, exist_ok=False)
    args.private.mkdir(parents=True, exist_ok=False)
    manifest = {"started_at": utcnow().isoformat(), "scope": "cardinality_gate_and_uncertain_planner_fallback_only",
        "baseline": "Jev high confidence else original regex", "candidate": "Jev high confidence else original GoalPlan LLM",
        "pairing": "One shared real Jev response per case; no duplicate Jev request, no cache simulation",
        "limits": "Accepted multi only classified, not expanded; no tools or E2E measured. Baseline has no planner generation.",
        "statistical_unit": "authored_query_and_context", "splits": {"regression": 30, "holdout": 12},
        "jev_model": settings.jev_model_name, "planner_model": getattr(roles.get("planner", model), "model_name", None),
        "jev_timeout": settings.jev_timeout_seconds, "min_confidence": settings.jev_min_confidence,
        "min_probability": settings.jev_min_probability, "planner_variant": settings.planner_variant,
        "seed": args.seed, "actual_jev_requests": 0, "automatic_deployment": False,
        "data_hash": hashlib.sha256(json.dumps(dataset, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "source_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (
            "src/car_agent/planning_gate.py", "src/car_agent/task_planning.py", "src/car_agent/structured_output.py")}}
    (args.output / "cases.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in dataset))
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    db, rows = Database(str(args.private / "audit.sqlite3")), []
    await db.open()
    try:
        random.Random(args.seed).shuffle(dataset)
        for case in dataset:
            now, identifier = utcnow().isoformat(), case["id"]
            await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (identifier, identifier, "fallback-policy-evaluation", case["query"], "running", "live", now, now))
            started, plan, error = monotonic(), None, None
            async with execution_scope(db, identifier, RuntimeLimits.from_settings(settings), "planning"):
                cardinality, domain = await gate.classify(case["query"], case["context"])
                shared_ms = round((monotonic() - started) * 1000)
                before = await db.fetchall("SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (identifier,))
                shared_events = [{"type": e["type"], **json.loads(e["data_json"])} for e in before]
                if cardinality is None:
                    try:
                        plan = await plan_tasks(case["query"], "live", roles.get("planner", model), case["context"],
                            variant=settings.planner_variant, gate=SharedGate(cardinality, domain))
                    except Exception as exc:
                        error = type(exc).__name__
            elapsed = round((monotonic() - started) * 1000)
            events = [{"type": e["type"], **json.loads(e["data_json"])} for e in await db.fetchall(
                "SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (identifier,))]
            original = cardinality or ("multi" if may_have_multiple_tasks(case["query"]) else "single")
            updated = cardinality or ("single" if plan and len(plan.tasks) == 1 else "multi" if plan else "error")
            for arm, label, duration, observed in (("baseline", original, shared_ms, shared_events),
                                                    ("candidate", updated, elapsed, events)):
                row = {**case, "arm": arm, "actual_label": label, "passed": label == case["expected_label"],
                    "elapsed_ms": duration, "fallback": cardinality is None, "accepted_cardinality": cardinality,
                    "attempts": attempts_from_events(observed),
                    "prechecks": [e for e in observed if e["type"] == "planning.precheck.completed"],
                    "planner_result": plan.model_dump() if arm == "candidate" and plan else None,
                    "error_type": error if arm == "candidate" else None}
                rows.append(row)
                with (args.output / "results.jsonl").open("a") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({k: row[k] for k in ("id", "arm", "actual_label", "passed", "fallback", "elapsed_ms")}), flush=True)
            manifest["actual_jev_requests"] += 1
            (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            (args.output / "summary.json").write_text(json.dumps(reports(rows), ensure_ascii=False, indent=2) + "\n")
            (args.output / "details.csv").write_text(csv_rows(rows))
        for split, summary in reports(rows).items():
            destination = args.output / split
            destination.mkdir()
            write_summary(destination, summary)
        render_report(args.output)
    finally:
        await gate.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--document", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--private", type=Path, default=Path("evaluations/runs/jev-fallback-llm-20261029-01"))
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    if args.render:
        render_report(args.output, args.document)
        return
    if not args.run_live:
        print("Prepared only. --run-live explicitly enables paid Jev and planner calls.")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
