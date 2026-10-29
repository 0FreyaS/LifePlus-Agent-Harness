"""Isolated real-provider model swap; no changes to the running application."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

from .jev_ab import percentile, read_e2e_rows, summarize, usage_summary

DEFAULT_PUBLIC = Path("evaluations/model_comparison/2026-10-29-deepseek-flash/v1")
DEFAULT_PRIVATE = Path("evaluations/runs/deepseek-flash-ab-20261029-01")


def cases():
    result = []
    for repeat in range(1, 4):
        result += [
            {"id": f"model-weather-r{repeat}", "group": "weather", "repeat_index": repeat, "query": "查询无锡今天天气",
             "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$today"}}],
             "user_policy": {}, "source": "authored_model_swap_v1"},
            {"id": f"model-chat-r{repeat}", "group": "chat", "repeat_index": repeat, "query": "介绍歌手陶喆",
             "goals": [{"domain": "chat", "tool": None, "arguments": {}, "answer_contains": "陶喆"}],
             "user_policy": {}, "source": "authored_model_swap_v1"},
        ]
    result += [
        {"id": "model-multi", "group": "multi", "query": "查无锡今天的天气，同时搜索周杰伦的歌曲",
         "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$today"}},
                   {"domain": "music", "tool": "search_music", "arguments": {"keyword": "周杰伦"}}],
         "user_policy": {}, "source": "authored_model_swap_v1"},
        {"id": "model-clarify", "group": "clarify", "query": "查询今天天气",
         "goals": [{"domain": "weather", "tool": "maps_weather", "arguments": {"city": "无锡", "forecast_date": "$today"}}],
         "user_policy": {"answers": {"city": "无锡"}, "require_fields": ["city"]}, "source": "authored_model_swap_v1"},
    ]
    return result


def stage_summary(rows):
    result = {}
    for arm in ("baseline", "candidate"):
        groups = defaultdict(list)
        for row in rows:
            if row["arm"] == arm:
                for attempt in row["attempts"]:
                    groups[attempt["stage"]].append(attempt)
        result[arm] = {name: {"calls": len(attempts), "p50_ms": percentile([a["elapsed_ms"] for a in attempts], .5),
            "p95_ms": percentile([a["elapsed_ms"] for a in attempts], .95), **usage_summary(attempts)}
            for name, attempts in groups.items()}
    return result


def metrics(rows):
    complete_ids = [case_id for case_id in sorted({row["id"] for row in rows})
        if {row["arm"] for row in rows if row["id"] == case_id} == {"baseline", "candidate"}
        and all(usage_summary(row["attempts"])["attempts_missing_usage"] == 0
            for row in rows if row["id"] == case_id)]
    return {"all": summarize(rows), "by_group": {name: summarize([r for r in rows if r["group"] == name])
        for name in sorted({r["group"] for r in rows})}, "stages": stage_summary(rows),
        "stages_by_group": {name: stage_summary([r for r in rows if r["group"] == name])
            for name in sorted({r["group"] for r in rows})},
        "complete_usage_pairs": {"ids": complete_ids,
            "summary": summarize([row for row in rows if row["id"] in complete_ids])}}


def render_report(public, document=None):
    public = Path(public)
    rows = [json.loads(line) for line in (public / "results.jsonl").read_text().splitlines()]
    frozen = [json.loads(line) for line in (public / "cases.jsonl").read_text().splitlines()]
    ids = {case["id"] for case in frozen}
    if len(rows) != 2 * len(ids) or any(len(selected := [r for r in rows if r["arm"] == arm]) != len(ids)
        or {r["id"] for r in selected} != ids for arm in ("baseline", "candidate")):
        raise ValueError("Incomplete paired model evidence")
    summary = metrics(rows)
    (public / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    labels = {"weather": "天气查询（同一句3轮）", "chat": "歌手介绍（同一句3轮）", "multi": "天气＋音乐（1例）",
        "clarify": "天气缺城市澄清（1例）", "all": "合计8次场景运行"}
    table = ["| 范围 | 完整通过/提交 Grok→DeepSeek | P50（秒） | P95（秒） | 总Token |", "|---|---|---|---|---|"]
    for group in ("weather", "chat", "multi", "clarify", "all"):
        arms = summary["all"]["arms"] if group == "all" else summary["by_group"][group]["arms"]
        a, b = arms["baseline"], arms["candidate"]
        def tokens(values):
            return str(values["total_tokens"]) if values["total_tokens"] is not None else f"≥{values['observed_total_tokens']}（{values['attempts_missing_usage']}次未知）"
        table.append(f"| {labels[group]} | {a['passed']}/{a['tested']} → {b['passed']}/{b['tested']} | "
            f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | {tokens(a)} → {tokens(b)} |")
    stages = ["| 单任务场景 | 模型阶段 | Grok P50 | DeepSeek P50 |", "|---|---|---:|---:|"]
    for group in ("weather", "chat"):
        for stage in sorted(set(summary["stages_by_group"][group]["baseline"]) | set(summary["stages_by_group"][group]["candidate"])):
            a = summary["stages_by_group"][group]["baseline"].get(stage, {})
            b = summary["stages_by_group"][group]["candidate"].get(stage, {})
            fmt = lambda value: f"{value/1000:.3f}秒" if value is not None else "未调用"
            stages.append(f"| {labels[group]} | {stage} | {fmt(a.get('p50_ms'))} | {fmt(b.get('p50_ms'))} |")
    known = summary["complete_usage_pairs"]
    a_tokens = known["summary"]["arms"]["baseline"]["total_tokens"]
    b_tokens = known["summary"]["arms"]["candidate"]["total_tokens"]
    token_note = (f"用量完整的{len(known['ids'])}组配对（包含两组Jev用量）：{a_tokens} → {b_tokens} Token，"
        f"减少{(1-b_tokens/a_tokens)*100:.2f}%。这只是已知用量子集；完整8组的Grok总量未知，不能给出精确全量降幅。"
        if a_tokens else "暂无完整配对Token统计。")
    missing = [{"id": row["id"], "arm": row["arm"], "stage": attempt["stage"],
        "error_type": attempt.get("error_type"), "elapsed_ms": attempt["elapsed_ms"]}
        for row in rows for attempt in row["attempts"] if usage_summary([attempt])["attempts_missing_usage"]]
    lines = ["# Grok与DeepSeek Flash关闭思考模式：真实模型替换对比", "",
        "日期：2026-10-29。A为原grok-4.3，B为deepseek-flash并显式关闭思考；planner/intent/response三角色均替换为相应组模型。本轮隔离试验当时未切换Docker及主.env；后续获批部署另记在docs/deepseek-rollout.md。", "",
        "## 条件与边界", "",
        "- DeepSeek独立API连通性探测200，返回模型deepseek-flash，显式thinking.type=disabled，探测无reasoning_content；探测用量9 Token单独存在，不混入配对任务消耗。",
        "- 通过OpenAI兼容接口extra_body传入thinking；默认不设置时不改变原Grok请求。已用真实SDK的MockTransport验证该字段出现在HTTP请求体，不是仅提示模型不要思考。",
        "- 两组均保留领域Jev，事件解释llm，单/多门控rules；相同Schema、提示词、上限、工具及存储实现。每场景每组独立进程与隔离数据库，串行交替AB/BA。",
        "- 实际走HTTP处理器、Graph、MCP与外部天气/音乐；不改用户业务库。澄清及候选选择由固定测试策略回复，无真实用户思考等待，不包含浏览器/TCP展示。",
        "- 天气与歌手介绍各重复3次，另有多任务及澄清各1例，共4类Query、8次场景运行、16条观察，不是8个独立真实用户。样本少，P95接近或等于最大值，不是生产SLA。",
        "- 比较同时改变了模型、供应商与思考配置，不能把所有差异单独归因于关闭思考。Token采用各供应商分词和reported total，包含Jev及失败/重试；Token下降不直接等于货币成本下降。",
        "- 完成判据为领域、关键参数、真实工具回执及澄清契约；天气不逐项核验气象事实，歌手介绍只检验领域与主题，不宣称已人工验证所有知识细节。", "",
        "## 本轮配对结果", "", *table, "",
        f"完整8组P50降低{summary['all']['p50_reduction']*100:.2f}%，P95降低{summary['all']['p95_reduction']*100:.2f}%；分位数沿用项目nearest-rank口径。",
        "", token_note, "",
        "未知用量尝试（不剔除其任务时延或完成结果）：" + json.dumps(missing, ensure_ascii=False), "",
        "其中Grok组歌手介绍第3轮的Jev超时后触发原LLM领域回退；其额外开销不是模型固有效果。天气3组均无此回退，仍有明显提速。",
        "", "## 刚才历史单次结果（不是本轮A基线）", "",
        "| 输入 | 历史Jev | 历史领域提参 | 历史回复 | 历史端到端 |", "|---|---:|---:|---:|---:|",
        "| 查询无锡今天天气 | 1.270秒 | 4.163秒 | 6.970秒 | 13.761秒 |",
        "| 介绍歌手陶喆 | 0.769秒 | 无额外调用 | 6.639秒 | 7.594秒 |", "",
        "历史数字来自本机data/jev-domain-rollout-smoke-20261029.json；用于对应之前讨论的现象，不与本轮多次重复混算。", "",
        "## 本轮阶段耗时", "", *stages, "",
        "各阶段分位数不能直接相加成端到端分位数；多任务并行时尤其不能将所有模型耗时求和当关键路径。Jev未替换，两组Jev耗时差异是调用波动，不是DeepSeek优化了Jev。", "",
        "## 文件与复跑", "",
        "cases.jsonl是冻结输入与目标；results.jsonl保留逐条模型尝试、用量、完成判据失败；summary.json含各组P50/P95、分阶段与Token；manifest.json不含Key；details.csv便于筛选。私有完整Trace/SQLite及日志在evaluations/runs/deepseek-flash-ab-20261029-01。", "",
        "私有.env.deepseek-ab已被Git忽略，600权限；不要上传或拷进镜像。本试验不自动部署；之后用户批准的DeepSeek切换与新的Jev基准对照属于独立后续操作。", "",
        "```bash", ".venv/bin/python -m evaluations.deepseek_model_ab --render",
        ".venv/bin/python -m evaluations.deepseek_model_ab --run-live --public <新公开证据目录> --private <新私有日志目录>",
        "```", "", "思考模式参数依据：[DeepSeek官方说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。"]
    for row in rows:
        if not row["passed"]:
            lines.append("\n失败保留：" + json.dumps({k: row[k] for k in ("id", "arm", "failures")}, ensure_ascii=False))
    text = "\n".join(lines) + "\n"
    (public / "README.md").write_text(text)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["id", "group", "arm", "model", "passed", "elapsed_ms", "total_tokens", "unknown_attempts"], lineterminator="\n")
    writer.writeheader()
    for row in rows:
        usage = usage_summary(row["attempts"])
        writer.writerow({**{k: row[k] for k in ("id", "group", "arm", "model", "passed", "elapsed_ms")},
            "total_tokens": usage["total_tokens"] if usage["total_tokens"] is not None else "unknown", "unknown_attempts": usage["attempts_missing_usage"]})
    (public / "details.csv").write_text(output.getvalue())
    if document:
        document = Path(document)
        start, end = "<!-- model-comparison:deepseek-flash:start -->", "<!-- model-comparison:deepseek-flash:end -->"
        note = (start + "\n\n### 2026-10-29：deepseek-flash关闭思考，对比原grok-4.3\n\n"
            + "领域Jev与业务流程保持不变，只替换原LLM三个角色；这轮隔离测试当时不自动切换服务，之后获批切换另记于deepseek-rollout.md。真实模型＋Graph＋MCP，共8组配对。\n\n"
            + "\n".join(table) + "\n\n本组不是此前Jev候选策略的A/B；天气/聊天重复各3次，另有多任务与澄清各1例。"
            + "关闭思考通过HTTP请求字段实现，不是提示词。结果不等于已证明知识回答100%准确或生产SLA。\n\n"
            + token_note + "\n\nGrok组歌手介绍第3轮Jev超时并回退LLM，已保留该次完整耗时；不能把这部分差异全归因于DeepSeek。\n\n"
            + f"[完整阶段分解、历史单次对照、数据与限制]({os.path.relpath(public, document.parent)}/README.md)。\n\n" + end)
        previous = document.read_text()
        if start in previous or end in previous:
            if previous.count(start) != 1 or previous.count(end) != 1 or previous.index(end) < previous.index(start):
                raise ValueError("Invalid model comparison note markers")
            previous = previous[:previous.index(start)] + note + previous[previous.index(end)+len(end):]
        else:
            previous = previous.rstrip() + "\n\n" + note + "\n"
        document.write_text(previous)
    return summary


async def run(args):
    from dotenv import dotenv_values
    from car_agent.config import Settings

    settings = Settings()
    profile = dotenv_values(args.profile)
    if not profile.get("DEEPSEEK_API_KEY") or not settings.model_api_key:
        raise ValueError("Both baseline and candidate credentials are required")
    args.public.mkdir(parents=True, exist_ok=False)
    args.private.mkdir(parents=True, exist_ok=False)
    dataset = args.public / "cases.jsonl"
    selected = cases()
    dataset.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in selected))
    manifest = {"scope": "real_model_graph_mcp_isolated_storage", "baseline_model": settings.model_name,
        "candidate_model": profile.get("DEEPSEEK_MODEL_NAME", "deepseek-flash"),
        "candidate_base_url": profile.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        "candidate_thinking": "disabled", "baseline_thinking_override": None,
        "domain_route_provider": "jev_cascade", "input_event_provider": "llm", "planning_gate_provider": "rules",
        "seed": args.seed, "order": "shuffled cases; alternating AB/BA", "concurrency": 1,
        "case_budget_seconds": 120, "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "repeated_query_groups": {"weather": 3, "chat": 3}, "additional_quality_scenarios": 2,
        "limitations": "Eight scenario-runs from four query families, not eight independent users. ASGI excludes browser/TCP. "
            "Model, provider and candidate thinking mode are changed together; not a pure model-only ablation.",
        "automatically_deployed": False, "completed_arms": 0}
    (args.public / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    random.Random(args.seed).shuffle(selected)
    rows = []
    for index, case in enumerate(selected):
        pair_metadata = {}
        for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
            candidate = arm == "candidate"
            model_name = manifest["candidate_model"] if candidate else settings.model_name
            env = {**os.environ, "MODEL_API_KEY": profile["DEEPSEEK_API_KEY"] if candidate else settings.model_api_key,
                "MODEL_BASE_URL": manifest["candidate_base_url"] if candidate else settings.model_base_url,
                "MODEL_NAME": model_name, "DOMAIN_ROUTE_PROVIDER": "jev_cascade", "INPUT_EVENT_PROVIDER": "llm",
                "PLANNING_GATE_PROVIDER": "rules", "PLANNER_MODEL_NAME": model_name, "INTENT_MODEL_NAME": model_name,
                "RESPONSE_MODEL_NAME": model_name, "MODEL_STREAM_USAGE": "true"}
            if candidate:
                env["MODEL_THINKING"] = "disabled"
            else:
                env.pop("MODEL_THINKING", None)
            directory = args.private / (case["id"] + "-" + arm)
            command = [sys.executable, "-m", "evaluations.completion", "--dataset", str(dataset), "--ids", case["id"],
                "--output", str(directory), "--concurrency", "1", "--case-budget", "120"]
            print(json.dumps({"id": case["id"], "arm": arm, "model": model_name, "status": "started"}), flush=True)
            with (args.private / (case["id"] + "-" + arm + ".log")).open("w") as log:
                process = await asyncio.create_subprocess_exec(*command, env=env, stdout=log, stderr=log)
                try:
                    async with asyncio.timeout(195):
                        await process.wait()
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    raise
            if not (directory / "results.jsonl").exists():
                raise RuntimeError("No completion evidence; inspect private runner log")
            metadata = json.loads((directory / "manifest.json").read_text())
            pair_metadata[arm] = metadata
            if metadata["execution_conditions"].get("model_thinking") != ("disabled" if candidate else None):
                raise RuntimeError("Thinking configuration did not reach completion runner")
            actual = read_e2e_rows(directory, arm)
            for raw in actual:
                row = {key: raw[key] for key in ("id", "arm", "group", "passed", "elapsed_ms", "attempts", "failures",
                    "goals_total", "goals_completed", "precheck_counts") if key in raw}
                row["model"] = model_name
                rows.append(row)
                with (args.public / "results.jsonl").open("a") as output:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({key: row[key] for key in ("id", "arm", "passed", "elapsed_ms", "failures")}), flush=True)
            manifest["completed_arms"] += 1
            (args.public / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            (args.public / "summary.json").write_text(json.dumps(metrics(rows), ensure_ascii=False, indent=2) + "\n")
        for field in ("source_sha256", "dataset_sha256", "selected_ids", "concurrency", "case_budget_seconds"):
            if pair_metadata["baseline"][field] != pair_metadata["candidate"][field]:
                raise RuntimeError("Unmatched paired conditions: " + field)
        a = pair_metadata["baseline"]["execution_conditions"]
        b = pair_metadata["candidate"]["execution_conditions"]
        if any(a[k] != b[k] for k in a if k not in {"model_base_url", "model_thinking"}):
            raise RuntimeError("Different execution conditions outside the selected model treatment")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--document", type=Path)
    parser.add_argument("--profile", type=Path, default=Path(".env.deepseek-ab"))
    parser.add_argument("--public", type=Path, default=DEFAULT_PUBLIC)
    parser.add_argument("--private", type=Path, default=DEFAULT_PRIVATE)
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    if args.render:
        render_report(args.public, args.document)
    elif args.run_live:
        asyncio.run(run(args))
    else:
        print("Prepared only. --run-live enables real provider and MCP calls.")


if __name__ == "__main__":
    main()
