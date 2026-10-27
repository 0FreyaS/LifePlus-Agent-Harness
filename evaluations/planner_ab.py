"""Real-model paired A/B pilot. Does not enable a candidate or execute write tools."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import time
from datetime import datetime
from pathlib import Path

from car_agent.config import Settings
from car_agent.model_roles import build_models
from car_agent.task_planning import plan_tasks
from evaluations.planner_cases import CASES, grade

NOW = datetime.fromisoformat("2026-10-27T10:00:00+08:00")
GATE = {"min_quality": 0.95, "max_new_failures": 0, "min_median_speedup": 0.15,
        "max_p95_ratio": 1.05, "bootstrap_lower_bound_gt": 0, "max_token_ratio": 1.0}


class MeteredModel:
    def __init__(self, model):
        self.model = model
        self.model_name = model.model_name
        self.attempts = []

    def with_structured_output(self, *args, **kwargs):
        runnable = self.model.with_structured_output(*args, **kwargs)
        owner = self

        class Call:
            async def ainvoke(self, messages):
                started = time.monotonic()
                record = {"input_characters": sum(len(item["content"]) for item in messages)}
                try:
                    value = await runnable.ainvoke(messages)
                    raw = value.get("raw") if isinstance(value, dict) else None
                    record["usage"] = getattr(raw, "usage_metadata", None)
                    record["finish_reason"] = (getattr(raw, "response_metadata", None) or {}).get("finish_reason")
                    record["parse_failed"] = bool(value.get("parsing_error")) if isinstance(value, dict) else False
                    return value
                except Exception as exc:
                    record["error"] = type(exc).__name__
                    causes = []
                    cause = exc.__cause__
                    while cause is not None and len(causes) < 5:
                        # Do not persist arbitrary exception text: it may contain headers or URLs.
                        causes.append({"type": type(cause).__name__})
                        cause = cause.__cause__
                    record["causes"] = causes
                    raise
                finally:
                    record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
                    owner.attempts.append(record)

        return Call()


def percentile(values, p):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def summarize(rows, manifest):
    arms = {}
    for variant in ("baseline", "compact"):
        items = [row for row in rows if row["variant"] == variant]
        usage = [attempt.get("usage") for row in items for attempt in row["attempts"]]
        tokens = {key: sum((item or {}).get(key, 0) for item in usage)
                  for key in ("input_tokens", "output_tokens", "total_tokens")}
        tokens["reasoning_tokens"] = sum((item or {}).get("output_token_details", {}).get("reasoning", 0) for item in usage)
        tokens["cache_read_tokens"] = sum((item or {}).get("input_token_details", {}).get("cache_read", 0) for item in usage)
        latency = [row["elapsed_ms"] for row in items]
        arms[variant] = {"runs": len(items), "passed": sum(row["grade"]["passed"] for row in items),
                         "errors": sum(bool(row.get("error")) for row in items),
                         "p50_ms": statistics.median(latency) if latency else None,
                         "p95_ms": percentile(latency, .95), "tokens": tokens,
                         "usage_known": bool(usage) and all(item is not None for item in usage),
                         "attempts": len(usage), "repairs": sum(max(0, len(row["attempts"]) - 1) for row in items),
                         "groups": {group: {"runs": sum(row["group"] == group for row in items),
                                            "passed": sum(row["group"] == group and row["grade"]["passed"] for row in items)}
                                    for group in sorted({row["group"] for row in items})}}
    paired = {}
    for row in rows:
        paired.setdefault((row["case_id"], row["repeat"]), {})[row["variant"]] = row
    complete = [pair for pair in paired.values() if len(pair) == 2]
    new_failures = [f"{pair['compact']['case_id']}:{pair['compact']['repeat']}" for pair in complete
                    if pair["baseline"]["grade"]["passed"] and not pair["compact"]["grade"]["passed"]]
    # Bootstrap at the case level, not repeated requests as independent samples.
    improvements = {}
    for pair in complete:
        a, b = pair["baseline"]["elapsed_ms"], pair["compact"]["elapsed_ms"]
        improvements.setdefault(pair["baseline"]["case_id"], []).append((a - b) / a)
    values = [statistics.mean(items) for items in improvements.values()]
    rng = random.Random(9271)
    boot = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(4000)) if values else []
    ci = [percentile(boot, .025), percentile(boot, .975)] if boot else [None, None]
    a, b = arms["baseline"], arms["compact"]
    full = len(complete) == manifest["cases"] * manifest["repeats"]
    speedup = 1 - b["p50_ms"] / a["p50_ms"] if a["p50_ms"] else 0
    checks = {
        "complete": full,
        "transport_valid": a["errors"] == 0 and b["errors"] == 0,
        "quality": bool(b["runs"]) and b["passed"] / b["runs"] >= GATE["min_quality"],
        "no_quality_regression": b["passed"] >= a["passed"] and not new_failures,
        "median_speedup": speedup >= GATE["min_median_speedup"],
        "p95_noninferior": bool(a["p95_ms"]) and b["p95_ms"] <= a["p95_ms"] * GATE["max_p95_ratio"],
        "paired_ci_positive": ci[0] is not None and ci[0] > 0,
        "tokens_nonincreasing": a["usage_known"] and b["usage_known"] and b["tokens"]["total_tokens"] <= a["tokens"]["total_tokens"],
    }
    return {"manifest": manifest, "arms": arms, "new_failures": new_failures,
            "median_speedup": speedup, "paired_case_median_speedup_ci95": ci,
            "gate_checks": checks, "recommend_enable": all(checks.values()),
            "limitations": ["30 hand-authored contract cases, not production traffic or task completion rate",
                            "same real model; no smaller-model quality claim", "no business tools or end-to-end P95 measured",
                            "provider caching is not controlled; balanced randomized pairs reduce time-order bias",
                            "usage totals are tokens, not a currency price or provider billing reconciliation"]}


def render_report(result):
    a, b = result["arms"]["baseline"], result["arms"]["compact"]
    valid = result["gate_checks"]["transport_valid"]
    lines = ["# 规划模型配对 A/B 实测", "",
             "## 决策", "",
             "候选满足全部预设门槛，可进入人工审核后的灰度。" if result["recommend_enable"] else "候选未满足全部门槛，不启用，默认版本仍为 baseline。",
             "工具不会自动修改配置或切换服务。", "",
             "## 测试条件", "",
             f"- 真实模型：`{result['manifest']['model']}`；温度 0；A/B 使用同一模型、同一冻结参考时间及上下文。",
             f"- {result['manifest']['cases']} 个不同场景，每个重复 {result['manifest']['repeats']} 轮；配对并发 {result['manifest']['pair_concurrency']}。",
             "- 随机打乱场景，并交替安排 AB/BA 顺序；同一对内两次请求顺序执行。",
             "- A：原提示词、2048 输出上限；B：去除 Schema 重复元数据、紧凑 JSON、独立规划配置及 1536 输出上限。",
             "- 输入业务规则、任务结构、依赖和槽位验证器相同；不是更换小模型的实验。",
             "- P50 为中位数，P95 为 nearest-rank；包括失败与格式修复耗时。", "",
             "## 结果", "",
             "| 指标 | A：baseline | B：compact |", "|---|---:|---:|",
             f"| 请求数 | {a['runs']} | {b['runs']} |",
             f"| 规划契约严格通过 | {a['passed']}/{a['runs']} | {b['passed']}/{b['runs']} |",
             f"| 执行异常 | {a['errors']} | {b['errors']} |",
             f"| 全请求 P50 | {a['p50_ms'] / 1000:.3f} 秒 | {b['p50_ms'] / 1000:.3f} 秒 |",
             f"| 全请求 P95 | {a['p95_ms'] / 1000:.3f} 秒 | {b['p95_ms'] / 1000:.3f} 秒 |",
             f"| 已报告 input tokens | {a['tokens']['input_tokens']} | {b['tokens']['input_tokens']} |",
             f"| 已报告 output tokens | {a['tokens']['output_tokens']} | {b['tokens']['output_tokens']} |",
             f"| 已报告 total tokens | {a['tokens']['total_tokens']} | {b['tokens']['total_tokens']} |",
             f"| reasoning tokens | {a['tokens']['reasoning_tokens']} | {b['tokens']['reasoning_tokens']} |",
             f"| cache read tokens | {a['tokens']['cache_read_tokens']} | {b['tokens']['cache_read_tokens']} |",
             f"| 用量是否完整 | {a['usage_known']} | {b['usage_known']} |",
             f"| 格式修复 | {a['repairs']} | {b['repairs']} |", "",
             ("连接异常使本轮性能比较无效；快速失败不代表规划更快。" if not valid else
              f"P50 相对下降 {result['median_speedup']:.2%}；正值表示 B 更快。"), "",
             "## 门槛检查", ""]
    labels = {"complete": "全部配对完成", "transport_valid": "无连接/调用异常污染比较",
              "quality": "B 严格通过率至少 95%", "no_quality_regression": "总通过数不下降且无新增配对失败",
              "median_speedup": "P50 至少下降 15%", "p95_noninferior": "B P95 不超过 A 的 105%",
              "paired_ci_positive": "按场景聚合的配对提速 bootstrap 95% 下界大于 0",
              "tokens_nonincreasing": "用量完整且总 tokens 不增加"}
    lines.extend(f"- {'通过' if value else '未通过'}：{labels[key]}。" for key, value in result["gate_checks"].items())
    lines.extend(["", "## 分组严格通过", "", "| 分组 | A | B |", "|---|---:|---:|"])
    for group in a["groups"]:
        left, right = a["groups"][group], b["groups"][group]
        lines.append(f"| {group} | {left['passed']}/{left['runs']} | {right['passed']}/{right['runs']} |")
    lines.extend(["", "新增退化配对：" + ("、".join(f"`{item}`" for item in result["new_failures"]) or "无；两组共有错误仍计入总体通过率。"), ""])
    lines.extend(["", "## 证据与边界", "",
                  "- `manifest.json` 固定条件和门槛；`cases.json` 保存标注；`results.jsonl` 保存每条实际计划、断言和用量；`summary.json` 可复算结果。",
                  "- 通过要求任务数、领域动作、标注槽位、不得推断字段、依赖边及结果绑定同时正确；任务 ID 和拓扑等价顺序不影响评分。",
                  "- 城市仅容许市后缀差异，其他字段采用冻结的严格契约，不把所有语义近似自动判对。应区分契约失败与实际业务失败。",
                  "- 30 个手工合成场景不是生产流量。重复请求不是新增独立样本，不能把 60 次请求当作 60 个独立用户场景。",
                  "- 未执行业务工具，未测端到端完成率、地图可见时间、首 token 或系统 P95；不得外推为生产 SLA。",
                  "- 提供商缓存不可控，AB/BA 只能减轻时间顺序偏差；Token 是供应商回传用量，不是已核验账单。",
                  "- 不自动改用未经测试的小模型；结构化修复仍最多一次，额外调用计入时间和用量。", ""])
    return "\n".join(lines)


async def run(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    selected = CASES[:args.cases]
    config = Settings()
    base, _ = build_models(config.model_copy(update={"planner_variant": "baseline", "planner_model_name": None,
                                                    "intent_model_name": None, "response_model_name": None}))
    _, candidates = build_models(config.model_copy(update={"planner_variant": "compact", "planner_model_name": None,
                                                          "intent_model_name": None, "response_model_name": None}))
    if base is None:
        raise RuntimeError("A real configured model is required")
    manifest = {"started_at": datetime.now().astimezone().isoformat(), "cases": len(selected),
                "repeats": args.repeats, "pair_concurrency": args.concurrency, "seed": args.seed,
                "model": config.model_name, "temperature": 0, "baseline_tokens_cap": config.model_max_output_tokens,
                "compact_tokens_cap": config.planner_max_output_tokens, "frozen_now": NOW.isoformat(),
                "gate": GATE, "dataset_sha256": hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "cases.json").write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    jobs = [(case, repeat) for repeat in range(args.repeats) for case in selected]
    rng = random.Random(args.seed)
    rng.shuffle(jobs)
    orders = {}
    for index, (case, repeat) in enumerate(jobs):
        orders[(case["id"], repeat)] = ["baseline", "compact"] if index % 2 == 0 else ["compact", "baseline"]
    rows = []
    sem = asyncio.Semaphore(args.concurrency)

    async def pair(case, repeat):
        async with sem:
            for variant in orders[(case["id"], repeat)]:
                meter = MeteredModel(base if variant == "baseline" else candidates["planner"])
                started = time.monotonic()
                row = {"case_id": case["id"], "group": case["group"], "repeat": repeat, "variant": variant,
                       "started_at": datetime.now().astimezone().isoformat()}
                try:
                    async with asyncio.timeout(config.runtime_planning_seconds):
                        plan = await plan_tasks(case["query"], "live", meter, case["memory_brief"], variant=variant, now=NOW)
                    row["plan"] = plan.model_dump() if plan else None
                    row["grade"] = grade(case, plan)
                except Exception as exc:
                    row["error"] = type(exc).__name__
                    row["grade"] = {"passed": False, "errors": [type(exc).__name__]}
                row["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
                row["attempts"] = meter.attempts
                rows.append(row)
                with (output / "results.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({key: row[key] for key in ("case_id", "repeat", "variant", "elapsed_ms", "grade")}, ensure_ascii=False), flush=True)

    await asyncio.gather(*(pair(case, repeat) for case, repeat in jobs))
    result = summarize(rows, manifest)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "report.md").write_text(render_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=int, default=30, choices=range(1, 31))
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--seed", type=int, default=270927)
    args = parser.parse_args()
    if args.repeats < 1 or args.concurrency < 1:
        parser.error("repeats and concurrency must be positive")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
