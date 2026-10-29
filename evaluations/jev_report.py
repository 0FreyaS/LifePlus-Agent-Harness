"""Rebuild a shareable report from real Jev pilot evidence, without credentials."""
import argparse
import json
from pathlib import Path

from .jev_ab import summarize


def read_rows(directory):
    return [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines() if line.strip()]


def cells(report):
    def branch(arm):
        token = str(arm["total_tokens"]) if arm["total_tokens"] is not None else f">= {arm['observed_total_tokens']}（缺{arm['attempts_missing_usage']}次）"
        return f"{arm['passed']}/{arm['tested']}", f"{arm['p50_ms'] / 1000:.3f}", f"{arm['p95_ms'] / 1000:.3f}", token
    a, b = report["arms"]["baseline"], report["arms"]["candidate"]
    av, bv = branch(a), branch(b)
    return " | ".join((av[0], bv[0], av[1] + " → " + bv[1], av[2] + " → " + bv[2], av[3] + " → " + bv[3]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=Path("evaluations/runs"))
    parser.add_argument("--global-run", default="jev-global-20261029-02")
    parser.add_argument("--output", type=Path, default=Path("docs/jev-ab-2026-10-29.md"))
    parser.add_argument("--evidence", type=Path, default=Path("docs/assets/jev-ab-20261029.json"))
    args = parser.parse_args()
    groups = {
        "领域开发集": read_rows(args.runs / "jev-domain-dev-20261029-01"),
        "领域留出集": read_rows(args.runs / "jev-domain-holdout-20261029-01"),
        "事件开发集全部输入": read_rows(args.runs / "jev-event-dev-20261029-01"),
        "事件留出集": read_rows(args.runs / "jev-event-holdout-20261029-01"),
        "真实全链路": read_rows(args.runs / args.global_run),
    }
    groups["事件开发集需模型子集"] = [row for row in groups["事件开发集全部输入"] if row["group"] == "requires_model"]
    reports = {name: summarize(rows) for name, rows in groups.items()}
    manifest = json.loads((args.runs / args.global_run / "manifest.json").read_text())
    global_report = reports["真实全链路"]
    if global_report["paired_cases"] != len(manifest["cases"]):
        raise ValueError("Global evaluation still incomplete")
    if not any(attempt.get("stage") == "domain_route_precheck" for row in groups["真实全链路"]
               if row["arm"] == "candidate" for attempt in row["attempts"]):
        raise ValueError("Candidate global run never invoked Jev domain routing")
    public = {"date": "2026-10-29", "baseline_model": "grok-4.3", "decision_model": "jev-1.14.0",
        "reports": reports, "global_manifest": manifest, "default_providers_changed": False,
        "excluded_run": {"directory": "jev-global-20261029-01", "reason": "per-task context omitted domain_router; not a valid both-module experiment"},
        "global_cases": [{**{k: row.get(k) for k in ("id", "arm", "passed", "elapsed_ms", "group", "goals_completed", "goals_total", "failures", "error_type", "precheck_counts")},
            "model_failures": [{k: attempt.get(k) for k in ("stage", "error_type", "elapsed_ms")}
                               for attempt in row["attempts"] if attempt.get("type") == "model.attempt.failed"]}
                         for row in groups["真实全链路"]]}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Jev 与原系统：局部模块和全链路配对实测", "", "测试日期：2026-10-29。原模型 `grok-4.3`，候选决策模型 `jev-1.14.0`。真实调用两类模型；全链路还调用实际 MCP 和隔离存储。默认仍为原系统。", "",
        "## 运行条件", "", "- 单次配对试验，不将小样本通过率当生产准确率。领域14条开发集、21条留出集；事件30条开发集、6条留出集。",
        "- Jev timeout 1.5秒，confidence门槛0.8、选中项概率门槛0.9；低分、defer与服务错误回退原模型。没有在留出集上调参。",
        "- 局部保持输入与上下文相同、交替AB/BA。全局12条独立场景，随机顺序、交替AB/BA、并发1，每场景/分支新建进程与隔离用户/数据库，120秒case budget。",
        "- A保留原领域路由和事件解释；全局B同时启用两个Jev级联。规划、领域提参、工具、确认、回复及业务校验保持相同实现。两组均开启已验证受支持的stream_usage，采集流式回复用量。",
        "- 全局覆盖天气、音乐、路线、保存行程、聊天、多任务、结果依赖、缺参、复合回复和四目标。没有浏览器/TCP传输或真实人工思考时间。", "",
        "## 配对结果", "", "P50/P95单位为秒，箭头为原系统到Jev级联。通过意味着通过对应模块的完整断言，事件包含目标任务、候选和附带需求；全链路要求全部用户目标通过回执核验。", "",
        "| 范围 | 原系统通过 | 候选通过 | P50秒 A→B | P95秒 A→B | 总Token A→B |", "|---|---:|---:|---:|---:|---|",
    ]
    for name in ("领域开发集", "领域留出集", "事件开发集全部输入", "事件开发集需模型子集", "事件留出集", "真实全链路"):
        lines.append("| " + name + " | " + cells(reports[name]) + " |")
    lines += ["", "Token口径优先使用供应商total_tokens；不存在该字段、但输入与输出齐全时才相加。推理Token不漏算、不重复叠加。`>=`是已知用量下界，未知次数包含无usage的超时或供应商错误。各供应商计数口径不同，不换算统一货币费用。", "",
        "## 分模块解释", "", "领域仲裁是本次最明确的局部提速点；留出集没有出现新增错分，但回退仍会抬高尾延迟。开发集原系统的一条失败为供应商429，不解释为语义理解能力下降。", "",
        "事件必须看需要模型的子集：原有规则直接命中的输入两边相同。候选回退率较高，额外判断可能增加耗时和Token；整体输入的中位数容易受规则样本比例影响，不能代表复杂事件提速。留出集出现原模型回退后连续格式错误，保留为整条请求失败。", "",
        "## 全局逐条结果", "", "| 场景 | 分支 | 全目标通过 | 秒 | 完成目标/目标总数 | 失败断言 |", "|---|---|---|---:|---:|---|"]
    for row in sorted(groups["真实全链路"], key=lambda row: (row["id"], row["arm"])):
        failures = "; ".join(row.get("failures", [])) or row.get("error_type", "")
        lines.append(f"| {row['id']} | {row['arm']} | {row['passed']} | {row['elapsed_ms'] / 1000:.3f} | {row.get('goals_completed', 0)}/{row.get('goals_total', 0)} | {failures} |")
    lines += ["", "## 故障归因与采用决定", "", "首轮`jev-global-20261029-01`配置了domain与events，但每任务RunContext遗漏domain_router，实际没有替换领域仲裁；它不计入主表。修复为复制完整dataclass上下文，并新增真实图回归和试验器Trace生效检查。当前主表使用修复后的02目录，且确实包含domain_route_precheck调用。", "",
        "首轮还发现流式回复未主动请求usage，两组的response用量缺失。已用真实模型验证stream_usage可返回输入、可见输出和推理用量，在两组同时开启后重跑；旧记录保留，不补写推测用量。", "",
        "试验同时暴露原有全局规划的稳定性问题：保存依赖结果时，LLM有时将bind_slots目标写成不支持的字段，GoalPlan在工具执行前拒绝。首轮四目标两组均有格式错误。正式02轮四目标的baseline连续两次OutputParserException，candidate为plan阶段OpenAIConnectionError；均发生在Jev领域或事件调用之前。成功率仍分别按11/12计算，但不能据此认定Jev解决或造成了规划错误。", "",
        "一次配对还受未修改的后续模型、网络和外部服务波动影响；不能只凭局部少一次LLM推断整体必然更快。失败更快不算体验改善，应同时核对完成率、成功子集耗时和分阶段Trace。", "",
        "默认provider不切换。领域仲裁可继续独立灰度验证；事件选择器保留实验状态，先降低回退开销并验证质量。补强全局规划绑定契约稳定性，再扩大端到端A/B。本次pilot不足以自动上线。", "",
        "## 分阶段用量与失败", ""]
    for name in ("领域留出集", "事件开发集需模型子集", "事件留出集", "真实全链路"):
        lines += [f"### {name}", "", "| 分支 | Stage | 模型尝试 | 已知Token下界 | 未知用量尝试 |", "|---|---|---:|---:|---:|"]
        for arm, report in reports[name]["arms"].items():
            for stage, usage in report["by_stage"].items():
                lines.append(f"| {arm} | {stage} | {usage['model_attempts']} | {usage['observed_total_tokens']} | {usage['attempts_missing_usage']} |")
        lines.append("")
    lines += ["## 证据与复跑", "", f"[脱敏汇总JSON](assets/{args.evidence.name})包含分组指标和全局逐条状态。原始manifest、results.jsonl、audit.sqlite3、runtime.sqlite3及完整Trace在本地evaluations/runs目录，不包含在公共汇总中。", "",
        "```bash", ".venv/bin/python -m evaluations.jev_ab --run-live --split holdout --repeats 1 --output evaluations/runs/new-domain-holdout",
        ".venv/bin/python -m evaluations.jev_event_ab --holdout --output evaluations/runs/new-event-holdout",
        ".venv/bin/python -m evaluations.jev_global_ab --candidate both --output evaluations/runs/new-global",
        ".venv/bin/python -m evaluations.jev_report", "```", "",
        "每次使用新目录。报告生成器默认读取本轮固定目录，复跑新目录时需更新报告选择或指定全局目录；不能覆盖旧记录、替换失败期望或把重复采样当新独立用户。", ""]
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"report": str(args.output), "evidence": str(args.evidence), "paired_global_cases": global_report["paired_cases"]}))


if __name__ == "__main__":
    main()
