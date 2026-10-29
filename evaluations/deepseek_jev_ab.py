"""Paired domain and pure-reply trials on the same non-thinking DeepSeek baseline."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .deepseek_model_ab import metrics

PUBLIC = Path("evaluations/jev/2026-10-29-deepseek/v1")
PRIVATE = Path("evaluations/runs/deepseek-jev-ab-20261029-01")
START = "<!-- deepseek-jev-v1:start -->"
END = "<!-- deepseek-jev-v1:end -->"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def freeze(path, values):
    content = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)
    if path.exists() and path.read_text() != content:
        raise ValueError("Frozen evidence changed; use a new experiment version")
    path.write_text(content)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def archive(source, destination, cases):
    destination.mkdir(parents=True, exist_ok=False)
    allowed = {"id", "repeat", "arm", "group", "passed", "elapsed_ms", "attempts", "fallback", "prechecks",
        "expected_domain", "actual_domain", "expected_label", "actual_label", "error_type", "failures",
        "source", "decision", "grade", "expected_filter", "actual_filter", "filter_label_correct", "unsafe_shortcut",
        "goals_total", "goals_completed", "precheck_counts"}
    rows = [{key: value for key, value in row.items() if key in allowed} for row in read_rows(source / "results.jsonl")]
    ids = {case["id"] for case in cases}
    if len(rows) != 2 * len(cases) or any(
        len(selected := [row for row in rows if row["arm"] == arm]) != len(cases)
        or {row["id"] for row in selected} != ids for arm in ("baseline", "candidate")):
        raise ValueError("Incomplete paired evidence")
    freeze(destination / "cases.jsonl", cases)
    freeze(destination / "results.jsonl", rows)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest.update(model="deepseek-flash", model_thinking="disabled", source_run=str(source))
    if (source / (cases[0]["id"] + "-baseline") / "manifest.json").exists():
        manifest["arm_conditions"] = {}
        for arm in ("baseline", "candidate"):
            child = json.loads((source / (cases[0]["id"] + "-" + arm) / "manifest.json").read_text())
            manifest["arm_conditions"][arm] = {key: child[key] for key in (
                "model", "role_models", "execution_conditions", "source_sha256", "domain_route_provider",
                "input_event_provider", "planning_gate_provider", "case_budget_seconds", "concurrency")}
    write_json(destination / "manifest.json", manifest)
    write_json(destination / "summary.json", metrics(rows))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["id", "group", "arm", "passed", "elapsed_ms", "total_tokens", "unknown_attempts", "source"], lineterminator="\n")
    from .jev_ab import usage_summary
    writer.writeheader()
    for row in rows:
        usage = usage_summary(row["attempts"])
        writer.writerow({**{key: row.get(key, "") for key in ("id", "group", "arm", "passed", "elapsed_ms", "source")},
            "total_tokens": usage["total_tokens"] if usage["total_tokens"] is not None else "unknown",
            "unknown_attempts": usage["attempts_missing_usage"]})
    (destination / "details.csv").write_text(output.getvalue())


async def run(args):
    from car_agent.config import Settings
    from .jev_ab import run as domain_run
    from .jev_cases import CASES as domain_cases
    from .jev_event_ab import run as event_run
    from .jev_global_ab import run as global_run
    from .resume_filter_cases import CASES as event_cases, E2E as event_e2e

    settings = Settings()
    if settings.model_name != "deepseek-flash" or settings.model_thinking != "disabled":
        raise ValueError("Requires the explicit non-thinking DeepSeek baseline")
    if any(value and value != settings.model_name for value in (
        settings.planner_model_name, settings.intent_model_name, settings.response_model_name)):
        raise ValueError("All roles must use the same selected baseline")
    args.public.mkdir(parents=True, exist_ok=False)
    args.private.mkdir(parents=True, exist_ok=False)
    datasets = args.public / "datasets"
    datasets.mkdir()
    domains = [case for case in domain_cases if case["split"] == "holdout"]
    domain_e2e = read_rows(Path("evaluations/data/jev-e2e-v1.jsonl"))
    for name, cases in (("domain", domains), ("domain-e2e", domain_e2e), ("events", event_cases), ("events-e2e", event_e2e)):
        freeze(datasets / (name + ".jsonl"), cases)
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "model": settings.model_name,
        "model_base_url": settings.model_base_url, "model_thinking": settings.model_thinking,
        "jev_model": settings.jev_model_name, "jev_timeout_seconds": settings.jev_timeout_seconds,
        "jev_min_confidence": settings.jev_min_confidence, "jev_min_probability": settings.jev_min_probability,
        "seed": args.seed, "sequence": ["local_domain", "global_domain", "local_events", "global_events"],
        "datasets": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in datasets.iterdir()},
        "rollout": "DeepSeek plus existing single-goal Jev; event policies are evidence-only",
        "completed": []}
    write_json(args.public / "manifest.json", manifest)
    common = {"seed": args.seed, "repeats": 1}
    jobs = [
        ("local_domain", domain_run, SimpleNamespace(**common, split="holdout", timeout=60,
            output=args.private / "local-domain"), "modules/domain", domains),
        ("global_domain", global_run, SimpleNamespace(seed=args.seed, candidate="domain", ids=None,
            dataset=datasets / "domain-e2e.jsonl", case_budget=120, output=args.private / "global-domain"),
            "end_to_end/domain", domain_e2e),
        ("local_events", event_run, SimpleNamespace(**common, strategy="jev_resume_filter", baseline_policy="llm_direct",
            dataset=datasets / "events.jsonl", holdout=False, front_holdout=False,
            output=args.private / "local-events"), "modules/events", event_cases),
        ("global_events", global_run, SimpleNamespace(seed=args.seed, candidate="resume_filter", ids=None,
            dataset=datasets / "events-e2e.jsonl", case_budget=120, output=args.private / "global-events"),
            "end_to_end/events", event_e2e),
    ]
    for name, runner, options, destination, cases in jobs:
        print(json.dumps({"scope": name, "status": "started"}), flush=True)
        await runner(options)
        archive(options.output, args.public / destination, cases)
        manifest["completed"].append(name)
        write_json(args.public / "manifest.json", manifest)
    render(args.public, args.document)


def compare_line(label, value):
    a, b = value["all"]["arms"]["baseline"], value["all"]["arms"]["candidate"]
    def tokens(v):
        return str(v["total_tokens"]) if v["total_tokens"] is not None else f"≥{v['observed_total_tokens']}（{v['attempts_missing_usage']}次未知）"
    return (f"| {label} | {a['passed']}/{a['tested']} → {b['passed']}/{b['tested']} | "
        f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | {tokens(a)} → {tokens(b)} |")


def render(public, document):
    public = Path(public)
    entries = [("local_domain", "modules/domain"), ("global_domain", "end_to_end/domain"),
        ("local_events", "modules/events"), ("global_events", "end_to_end/events")]
    rows = {name: read_rows(public / folder / "results.jsonl") for name, folder in entries}
    values = {name: metrics(items) for name, items in rows.items()}
    write_json(public / "summary.json", values)
    header = ["| 范围 | 完整通过/提交 A→B | P50秒 | P95秒 | 总Token |", "|---|---|---|---|---|"]
    lines = ["# DeepSeek关闭思考基准：Jev领域仲裁与纯回复过滤", "",
        "## 不混淆的四组实验", "",
        "1. 领域局部21条：A为DeepSeek领域分类，B为Jev高分接管＋DeepSeek回退。",
        "2. 领域全局12场景：仅改变DOMAIN_ROUTE_PROVIDER=llm/jev_cascade；事件llm、门控rules固定。",
        "3. 事件局部36条：A为自由文本直接DeepSeek（llm_direct），B为Jev纯回复过滤＋DeepSeek（jev_resume_filter）。",
        "4. 事件全局5场景：两组领域均Jev、门控均rules，只改变上述事件策略；结构化卡片保持宿主协议。", "",
        "真实请求前冻结数据，随机顺序交替AB/BA，四组串行。各场景一次，小样本、人工模板复用，不是生产SLA。",
        "所有LLM角色均deepseek-flash，thinking.type=disabled；原阈值与1.5秒Jev超时未调整。失败、重试及未知用量均保留。",
        "局部不调用业务工具；全局真实Graph、MCP、外部工具和隔离存储，人工回复由测试策略自动提交，不计用户思考/浏览器展示。",
        "本轮复用此前已查看过的冻结测试样本，属于回归对比，不宣称全新盲测或真实用户分布。", "",
        "## Jev单任务领域收益", "", *header,
        compare_line("局部领域21条", values["local_domain"]), compare_line("全链路12场景", values["global_domain"]), "",
        "旧Grok局部21条：21/21→21/21，P50 3.551→0.548秒，P95 5.684→1.481秒，Token15161→16575。旧值属于另一次模型/网络条件，不能当成DeepSeek基准。", "",
        "## 自由文本是否需要事件LLM", "", *header,
        compare_line("完整事件36条", values["local_events"])]
    for group, label in (("pure_reply", "纯回复12条"), ("compound", "恢复＋新任务12条"), ("boundary", "边界12条")):
        lines.append(compare_line(label, metrics([row for row in rows["local_events"] if row["group"] == group])))
    lines += [compare_line("全链路5场景", values["global_events"]), "",
        "A是用户要求的直接LLM方案，不是现在线上规则优先方案。Jev只判resume_only/needs_llm，不创建任务，不授予权限。",
        "只有高分且宿主能唯一绑定完整原文到有效待恢复任务，B才跳过事件LLM；复合、歧义、否定、过期和低分仍交DeepSeek。",
        "高校候选是局部人工快照；真实5场景覆盖天气/音乐，不宣称实时高德已验证高校候选简称。", "",
        "## 接管、失败与归因", ""]
    candidate = [row for row in rows["local_events"] if row["arm"] == "candidate"]
    labeled = [row for row in candidate if row.get("actual_filter") is not None]
    safety = {"pure_accepted": sum(row.get("source") == "jev" for row in candidate if row["group"] == "pure_reply"),
        "pure_submitted": sum(row["group"] == "pure_reply" for row in candidate),
        "unsafe_shortcuts": sum(bool(row.get("unsafe_shortcut")) for row in candidate),
        "non_pure_submitted": sum(row["group"] != "pure_reply" for row in candidate),
        "raw_filter_returned": len(labeled),
        "raw_filter_labels_correct": sum(bool(row.get("filter_label_correct")) for row in labeled),
        "llm_attempts": {arm: sum(attempt["stage"] == "input_events" for row in rows["local_events"]
            if row["arm"] == arm for attempt in row["attempts"]) for arm in ("baseline", "candidate")}}
    write_json(public / "event-coverage.json", safety)
    lines += ["```json", json.dumps(safety, ensure_ascii=False, indent=2), "```", ""]
    lines += ["Jev原始标签正确、通过门槛接管、最终完整事件正确是三个不同分母；标签返回但低分回退不能算作接管，也不把回退LLM错误说成Jev标签错误。", ""]
    lines += ["### 启用建议", "",
        "保留此前已获批的单领域Jev，但把中位数收益、尾部收益和增加的Token分别看，不以局部分类提升替代全局任务完成率。",
        "本批事件过滤没有减少全局事件LLM调用，增加的前置HTTP请求与回退抵消了少数纯回复的收益，不建议启用。保持当前规则优先＋DeepSeek解释，后续再单独验证可唯一绑定时零模型恢复的覆盖率。", ""]
    for case_id in ("pure01", "compound01"):
        selected = [row for row in rows["local_events"] if row["id"] == case_id]
        lines += [f"### {case_id}", "", "```json", json.dumps(selected, ensure_ascii=False, indent=2), "```", ""]
    for name, items in rows.items():
        value = values[name]
        a, b = value["all"]["arms"]["baseline"], value["all"]["arms"]["candidate"]
        lines += [f"### {name}", "",
            f"P50变化：{(b['p50_ms']/a['p50_ms']-1)*100:+.2f}%；P95变化：{(b['p95_ms']/a['p95_ms']-1)*100:+.2f}%。负数表示耗时下降。"]
        known = value["complete_usage_pairs"]
        ka, kb = known["summary"]["arms"]["baseline"], known["summary"]["arms"]["candidate"]
        if ka["total_tokens"]:
            lines += [f"用量完整交集{len(known['ids'])}组：{ka['total_tokens']}→{kb['total_tokens']} Token，"
                f"变化{(kb['total_tokens']/ka['total_tokens']-1)*100:+.2f}%；不代替全体未知总量。"]
        lines += ["阶段尝试数、P50/P95与用量见对应summary.json；阶段时长不能直接相加当并行关键路径。"]
        for row in items:
            if not row["passed"]:
                lines += ["失败保留：`" + json.dumps({key: row.get(key) for key in (
                    "id", "arm", "source", "error_type", "grade", "failures")}, ensure_ascii=False) + "`。"]
        lines += [""]
    lines += ["## 运行与数据边界", "",
        "DeepSeek及此前已获批的领域Jev部署决定与本实验分开；本轮不自动启用事件过滤。Token是供应商reported total（含失败与回退），不是统一分词或货币费用。",
        "datasets/保存输入期望；modules/、end_to_end/分别有cases/results JSONL、details.csv、summary与脱敏manifest。原始SQLite与日志在私有evaluations/runs/deepseek-jev-ab-20261029-01。Key不提交。", "",
        "```bash", ".venv/bin/python -m evaluations.deepseek_jev_ab --render",
        ".venv/bin/python -m evaluations.deepseek_jev_ab --run-live --public <新公开目录> --private <新私有目录>", "```", "",
        "报告复算不调用API；真实试验需新目录并会消耗供应商额度。"]
    (public / "README.md").write_text("\n".join(lines) + "\n")
    if document:
        path = Path(document)
        note = ["## DeepSeek基准下的Jev实测（2026-10-29）", "",
            "LLM统一deepseek-flash关闭思考，固定模型/提示词/阈值；领域与事件分别配对，不把Grok历史收益当成DeepSeek收益。", "",
            *header, compare_line("领域21条", values["local_domain"]), compare_line("领域全局12例", values["global_domain"]),
            compare_line("自由文本事件36条", values["local_events"]),
            compare_line("其中纯回复12条", metrics([row for row in rows["local_events"] if row["group"] == "pure_reply"])),
            compare_line("其中恢复＋新任务12条", metrics([row for row in rows["local_events"] if row["group"] == "compound"])),
            compare_line("文本事件全局5例", values["global_events"]), "",
            f"纯回复Jev接管{safety['pure_accepted']}/{safety['pure_submitted']}；复合/边界误走捷径{safety['unsafe_shortcuts']}/{safety['non_pure_submitted']}。接管率与最终准确率分别统计。",
            "具体例子：‘无锡校区吧’事件理解1.061→0.395秒、B来源jev；‘无锡校区吧，顺便查下明天无锡天气’0.807→0.932秒、两组均走LLM并完整保留新需求。上述为本批对应单例，不推广为总体收益。",
            "失败归因：领域全局两组都在天气结果保存、四目标依赖规划两例发生结构化解析失败；事件两组都在取消和带引号词义提问上过度澄清。所有失败留在分母，不归因为Jev修复或退化。",
            "事件A为直接DeepSeek，不是当前规则优先基线；实验事件策略不自动启用。小样本单次，失败不剔除、未知用量不当0，不承诺生产SLA。", "",
            f"[分组结果、具体两例、阶段耗时、全部数据与失败证据]({os.path.relpath(public, path.parent)}/README.md)。"]
        section = START + "\n\n" + "\n".join(note) + "\n\n" + END
        content = path.read_text()
        if START in content or END in content:
            if content.count(START) != 1 or content.count(END) != 1 or content.index(END) < content.index(START):
                raise ValueError("Invalid DeepSeek Jev note markers")
            content = content[:content.index(START)] + section + content[content.index(END)+len(END):]
        else:
            content = content.rstrip() + "\n\n" + section + "\n"
        path.write_text(content)
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--public", type=Path, default=PUBLIC)
    parser.add_argument("--private", type=Path, default=PRIVATE)
    parser.add_argument("--document", type=Path, default=Path("docs/jev-front-ab-2026-10-29.md"))
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    if args.run_live:
        asyncio.run(run(args))
    elif args.render:
        render(args.public, args.document)
    else:
        print("Prepared only; --run-live explicitly enables paid calls.")


if __name__ == "__main__":
    main()
