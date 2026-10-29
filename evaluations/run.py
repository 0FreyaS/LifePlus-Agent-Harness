"""Run with: .venv/bin/python -m evaluations.run --groups all"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from car_agent.adapters.mcp_client import LocalMCPClient
from car_agent.config import get_settings
from car_agent.harness import resolve_intent, resolve_domain_intent
from car_agent.task_planning import plan_tasks

from .runtime import RuntimeLab

TZ = timezone(timedelta(hours=8))
MODEL_GROUPS = {"single_task", "multi_task", "slot_extraction", "clarification"}


class Usage(BaseCallbackHandler):
    def __init__(self):
        self.input_tokens = self.output_tokens = self.responses = self.missing = 0

    def on_llm_end(self, response, **_):
        for generation in response.generations:
            message = generation[0].message
            usage = getattr(message, "usage_metadata", None)
            self.responses += 1
            if usage:
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)
            else:
                self.missing += 1

    def value(self):
        return {"responses": self.responses, "input_tokens": self.input_tokens if self.responses > self.missing else None,
                "output_tokens": self.output_tokens if self.responses > self.missing else None,
                "responses_missing_usage": self.missing}


def normalize(value, key=""):
    if isinstance(value, str):
        value = "".join(value.split()).replace("，", ",")
        if key == "city":
            value = value.removesuffix("市")
    return value


def expected_slots(slots, anchor):
    dates = {"$today": anchor.date().isoformat(), "$tomorrow": (anchor.date() + timedelta(days=1)).isoformat(),
             "$day_after": (anchor.date() + timedelta(days=2)).isoformat()}
    for alias, days, hour in (("$tomorrow9", 1, 9), ("$day_after15", 2, 15), ("$tomorrow20", 1, 20)):
        dates[alias] = (anchor + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()
    return {key: dates.get(value, value) if isinstance(value, str) else value for key, value in slots.items()}


def score_decision(case, prediction, anchor):
    expected = case["expected"]
    metrics = {}
    checks = []
    if "domain" in expected:
        good = prediction.get("domain") == expected["domain"]
        metrics["domain_accuracy"] = [int(good), 1]
        checks.append(good)
    if "action" in expected:
        good = prediction.get("action") == expected["action"]
        metrics["intent_exact"] = [int(all(checks) and good), 1]
        checks.append(good)
    if "slots" in expected:
        targets = expected_slots(expected["slots"], anchor)
        predicted = prediction.get("slots", {})
        matches = {}
        for key, target in targets.items():
            actual = predicted.get(key)
            if target is None:
                good = actual in (None, "")
            elif key == "remind_at":
                try:
                    good = datetime.fromisoformat(actual) == datetime.fromisoformat(target)
                except (ValueError, TypeError):
                    good = False
            else:
                good = normalize(actual, key) == normalize(target, key)
            matches[key] = good
        metrics["annotated_slot_accuracy"] = [sum(matches.values()), len(matches)]
        metrics["annotated_slots_exact"] = [int(all(matches.values())), 1]
        checks.append(all(matches.values()))
    return all(checks), metrics


def score_plan(case, tasks):
    expected = case["expected"]
    target, actual = Counter(expected["domains"]), Counter(task["domain"] for task in tasks)
    matched = sum((target & actual).values())
    exact = target == actual
    if expected["dependency"] == "independent":
        dependency_ok = not any(task.get("depends_on") or task.get("bind_slots") for task in tasks)
    else:
        roots = {task["id"] for task in tasks if task["domain"] == expected["domains"][0]}
        dependency_ok = any(task["domain"] == expected["domains"][-1]
            and roots.intersection(task.get("depends_on", []))
            and any(path.split(".")[0] in roots for path in task.get("bind_slots", {}).values()) for task in tasks)
    return exact and dependency_ok, {
        "task_domain_multiset_exact": [int(exact), 1], "task_domain_precision": [matched, len(tasks)],
        "task_domain_recall": [matched, len(expected["domains"])],
        "dependency_policy_accuracy": [int(dependency_ok), 1],
    }


def error_metrics(case):
    """Failures stay in accuracy denominators instead of inflating successful-only scores."""
    group, expected = case["group"], case["expected"]
    if group == "single_task":
        return {"domain_accuracy": [0, 1], "intent_exact": [0, 1]}
    if group == "slot_extraction":
        return {"domain_accuracy": [0, 1], "annotated_slots_exact": [0, 1],
                "annotated_slot_accuracy": [0, len(expected["slots"])]}
    if group == "multi_task":
        return {"task_domain_multiset_exact": [0, 1], "task_domain_precision": [0, 0],
                "task_domain_recall": [0, len(expected["domains"])], "dependency_policy_accuracy": [0, 1]}
    metrics = {"clarification_field_accuracy": [0, 1], "clarification_runtime_error": [1, 1]}
    if expected["missing_field"] is not None:
        metrics.update(necessary_clarification_recall=[0, 1], necessary_clarification_correct_field_recall=[0, 1])
    else:
        # No output is not proof of a correct no-clarification decision.
        metrics["unnecessary_clarification_rate"] = [0, 1]
        metrics["complete_input_runtime_error"] = [1, 1]
    return metrics


def percentile(values, p):
    """Nearest-rank quantile, including failures/timeouts."""
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def wilson(successes, count):
    if not count:
        return None
    z = 1.96
    rate = successes / count
    denom = 1 + z * z / count
    middle = (rate + z * z / (2 * count)) / denom
    margin = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denom
    return [round(middle - margin, 4), round(middle + margin, 4)]


def summarize(rows, cases, metadata):
    groups = {}
    for group in sorted({case["group"] for case in cases}):
        subset = [row for row in rows if row["group"] == group]
        metrics = defaultdict(lambda: [0, 0])
        for row in subset:
            for name, (num, den) in row.get("metrics", {}).items():
                metrics[name][0] += num
                metrics[name][1] += den
        passed = sum(row["passed"] for row in subset)
        groups[group] = {"expected_cases": sum(case["group"] == group for case in cases),
            "executed": len(subset), "passed": passed,
            "pass_rate": passed / len(subset) if subset else None,
            "approx_case_pass_wilson95": wilson(passed, len(subset)),
            "p50_ms": percentile([row["elapsed_ms"] for row in subset], .5),
            "p95_ms": percentile([row["elapsed_ms"] for row in subset], .95),
            "metrics": {name: {"numerator": num, "denominator": den, "value": num / den if den else None} for name, (num, den) in metrics.items()},
            "failed_ids": [row["id"] for row in subset if not row["passed"]]}
    return {**metadata, "groups": groups, "executed": len(rows), "passed": sum(row["passed"] for row in rows),
            "usage": {key: sum(row.get("usage", {}).get(key) or 0 for row in rows)
                      for key in ("responses", "input_tokens", "output_tokens", "responses_missing_usage")}}


def write_report(path, report):
    lines = ["# LifePlus 分场景评测实测报告", "", f"- 数据集 SHA256：`{report['dataset_sha256']}`",
             f"- 运行开始：{report['started_at']}；模型：`{report['model']}`；模型并发：{report['concurrency']}",
             f"- 已执行 {report['executed']} 条，通过 {report['passed']} 条。未执行不计为成功。",
             "- 数据均为人工设计的合成场景，不是线上流量；每类 30 条，仅作首版基线。",
             "- 模型类测试实际路由/拆分能力；受控流程测试使用固定工具回执；真实工具类实际调用 MCP。三者不混称端到端完成率。",
             "", "| 场景 | 已执行/应执行 | 通过 | 通过率 | P50(ms) | P95(ms) |", "|---|---:|---:|---:|---:|---:|"]
    for name, group in report["groups"].items():
        rate = f"{group['pass_rate']:.1%}" if group["pass_rate"] is not None else "未执行"
        lines.append(f"| {name} | {group['executed']}/{group['expected_cases']} | {group['passed']} | {rate} | {group['p50_ms']} | {group['p95_ms']} |")
    lines.extend(["", "## 分项指标", "", "| 场景 | 指标 | 分子/分母 | 实测 |", "|---|---|---:|---:|"])
    for name, group in report["groups"].items():
        for metric, values in group["metrics"].items():
            value = f"{values['value']:.1%}" if values["value"] is not None else "N/A"
            lines.append(f"| {name} | {metric} | {values['numerator']}/{values['denominator']} | {value} |")
    lines.extend(["", "## 失败样本", ""])
    for name, group in report["groups"].items():
        if group["failed_ids"]:
            lines.append(f"- {name}：" + "、".join(group["failed_ids"]))
    lines.extend(["", "## 口径与限制", "",
        "- single_task：单次真实模型调用后的领域与动作同时正确；不是业务端到端完成率。",
        "- multi_task：领域任务多重集合一致且依赖策略正确；精确率/召回率按任务数统计，不代表动作、槽位全部正确。需依赖的样本同时检查依赖边与输出绑定。",
        "- slot_extraction：仅对已标注字段严格比较，数字单位、城市后缀、ISO 时间做明确规范化；非标注字段不纳入，不能称全槽位 F1。空值用来检查禁止臆造。",
        "- clarification：真实模型路由后进入实际图节点，检查缺参澄清；15 条必要澄清、15 条完整信息对照；不覆盖工具返回歧义导致的候选选择。",
        "- candidate_selection：固定工具候选，验证索引、非法选择、选中商家 ID 绑定与空/单候选；不是自然语言实体识别准确率。",
        "- recovery：真实 SQLite Checkpoint 和队列，写回执为受控值。重新打开检查点不等同进程/服务器故障恢复，也未测 PostgreSQL。",
        "- tool_failure：受控失败、超时、瞬态重试、依赖阻断、快分支先完成。超时为加速测试注入预算，不是生产超时配置建议。",
        "- real_tools：实际 MCP 响应结构、业务证据和非模拟标志检查；天气不对照实况、音乐不验证版权播放、商家不验证实时营业。写入只进入临时评测数据库。",
        "- P95 使用 nearest-rank，含失败和超时。模型组是理解阶段耗时，工具组是调用耗时，受控组是测试步骤总耗时；不得相加或当作用户端到端时延。人工等待不参与。",
        "- 每组仅 30 条，100% 的 Wilson 95% 区间下界约 88.6%，且模板相关性会削弱独立同分布假设，不能证明 95%/98% 线上 SLA。",
        "- 使用供应商返回 Token usage；缺失标记单独计数，不估造货币成本。本轮未进行人工 Rubric 或独立 LLM judge 质量打分。",
        "", "## Token 使用", "", "```json", json.dumps(report["usage"], ensure_ascii=False, indent=2), "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


async def model_case(case, llm, lab, anchor):
    if case["group"] == "multi_task":
        plan = await plan_tasks(case["query"], "live", llm)
        if plan:
            tasks = [task.model_dump() for task in plan.tasks]
        else:
            decision = await resolve_intent({"original_query": case["query"], "mode": "live"}, {}, SimpleNamespace(context=SimpleNamespace(llm=llm)))
            tasks = [{"id": "single", **decision}]
        passed, metrics = score_plan(case, tasks)
        return passed, metrics, {"tasks": tasks}
    state = {"original_query": case["query"], "mode": "live"}
    runtime = SimpleNamespace(context=SimpleNamespace(llm=llm))
    state.update(await resolve_intent(state, {}, runtime))
    decision = await resolve_domain_intent(state, {}, runtime)
    if case["group"] == "clarification":
        result = await lab.clarification(case, decision)
        expected, actual = case["expected"]["missing_field"], result["missing_field"]
        necessary = expected is not None
        metrics = {"clarification_field_accuracy": [int(actual == expected), 1]}
        metrics["necessary_clarification_recall" if necessary else "unnecessary_clarification_rate"] = [int(actual is not None), 1]
        if necessary:
            metrics["necessary_clarification_correct_field_recall"] = [int(actual == expected), 1]
        return actual == expected, metrics, {"decision": decision, "graph": result}
    passed, metrics = score_decision(case, decision, anchor)
    return passed, metrics, decision


async def run(args):
    dataset = Path(args.dataset)
    data = dataset.read_bytes()
    cases = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate dataset IDs")
    selected = set(args.groups.split(",")) if args.groups != "all" else {case["group"] for case in cases}
    unknown = selected - {case["group"] for case in cases}
    if unknown:
        raise ValueError(f"Unknown groups: {sorted(unknown)}")
    cases = [case for case in cases if case["group"] in selected]
    settings = get_settings()
    anchor = datetime.now(TZ)
    output = Path(args.output or f"evaluations/runs/{anchor.strftime('%Y%m%d-%H%M%S')}")
    output.mkdir(parents=True, exist_ok=True)
    metadata = {"version": 1, "dataset_sha256": hashlib.sha256(data).hexdigest(),
        "started_at": anchor.isoformat(), "model": settings.model_name,
        "concurrency": args.concurrency, "selected_groups": sorted(selected),
        "real_tool_min_interval_seconds": args.tool_interval,
        "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
            "src/car_agent/harness.py", "src/car_agent/task_planning.py", "src/car_agent/tool_queue.py",
            "evaluations/dataset.py", "evaluations/runtime.py", "evaluations/run.py")},
        "scope": "component_evaluation_not_end_to_end_sla"}
    manifest = output / "manifest.json"
    rows_path = output / "results.jsonl"
    if manifest.exists():
        old = json.loads(manifest.read_text())
        if not args.resume or any(old.get(key) != metadata[key] for key in ("dataset_sha256", "model", "selected_groups", "concurrency", "source_sha256", "real_tool_min_interval_seconds")):
            raise ValueError("Existing run: use compatible --resume or a new output directory")
        if old["started_at"][:10] != anchor.isoformat()[:10]:
            raise ValueError("Relative-date run cannot resume on another calendar day")
        metadata = old
        anchor = datetime.fromisoformat(old["started_at"])
    else:
        manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = [json.loads(line) for line in rows_path.read_text().splitlines()] if rows_path.exists() else []
    done = {row["id"] for row in rows}
    semaphore = asyncio.Semaphore(args.concurrency)
    base_llm = None
    if selected & MODEL_GROUPS:
        if not settings.model_api_key or not settings.model_base_url or not settings.model_name:
            raise RuntimeError("Real model configuration is missing; never fall back to demo")
        base_llm = ChatOpenAI(base_url=settings.model_base_url, api_key=settings.model_api_key,
            model=settings.model_name, temperature=0, timeout=25, max_retries=1)

    def persist(row):
        rows.append(row)
        with rows_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        report = summarize(rows, cases, metadata)
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_report(output / "report.md", report)
        print(f"{row['id']} {'PASS' if row['passed'] else 'FAIL'} {row['elapsed_ms']}ms", flush=True)

    async with RuntimeLab() as lab:
        async def evaluate(case):
            async with semaphore:
                start = perf_counter()
                usage = Usage()
                row = {"id": case["id"], "group": case["group"], "runner": case["runner"],
                       "query": case["query"], "expected": case["expected"]}
                try:
                    llm = base_llm.model_copy(update={"callbacks": [usage]})
                    passed, metrics, prediction = await asyncio.wait_for(model_case(case, llm, lab, anchor), 75)
                    row.update(passed=passed, metrics=metrics, prediction=prediction)
                except Exception as exc:
                    error = str(exc).replace(settings.model_api_key or "__unset_secret__", "[REDACTED]")
                    row.update(passed=False, error_type=type(exc).__name__, error=error[:1200], metrics=error_metrics(case))
                row.update(elapsed_ms=round((perf_counter() - start) * 1000), usage=usage.value())
                persist(row)

        await asyncio.gather(*(evaluate(case) for case in cases if case["group"] in MODEL_GROUPS and case["id"] not in done))
        for case in cases:
            if case["id"] in done or case["group"] in MODEL_GROUPS or case["group"] == "real_tools":
                continue
            start = perf_counter()
            row = {"id": case["id"], "group": case["group"], "runner": case["runner"], "query": case["query"], "expected": case["expected"]}
            try:
                fn = {"candidate_selection": lab.selection, "recovery": lab.recovery, "tool_failure": lab.fault}[case["group"]]
                prediction = await asyncio.wait_for(fn(case), 15)
                row.update(passed=True, prediction=prediction, metrics={"scenario_assertion_pass": [1, 1]})
            except Exception as exc:
                row.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:1200], metrics={"scenario_assertion_pass": [0, 1]})
            row["elapsed_ms"] = round((perf_counter() - start) * 1000)
            persist(row)
    if "real_tools" in selected:
        with TemporaryDirectory(prefix="lifeplus-eval-mcp-") as temporary:
            environment = dict(os.environ, LIFEPLUS_DATA_PATH=str(Path(temporary) / "tools.sqlite3"))
            if settings.amap_maps_api_key:
                environment["AMAP_MAPS_API_KEY"] = settings.amap_maps_api_key
            client = LocalMCPClient(environment)
            try:
                await client.open()
                # A resumed run gets a new isolated DB; reconstruct only prerequisite eval writes.
                for previous in rows:
                    if (previous["group"] == "real_tools" and previous["passed"]
                            and previous["expected"]["tool"] == "save_itinerary"):
                        await client.call("save_itinerary", previous["expected"]["args"])
                for case in cases:
                    if case["group"] != "real_tools" or case["id"] in done:
                        continue
                    start = perf_counter()
                    row = {"id": case["id"], "group": case["group"], "runner": "real_mcp", "query": case["query"], "expected": case["expected"]}
                    try:
                        tool, arguments = case["expected"]["tool"], case["expected"]["args"]
                        prediction = await asyncio.wait_for(client.call(tool, arguments), 35)
                        valid = prediction.get("ok") is True and not prediction.get("simulated")
                        if tool in {"maps_search", "search_music", "maps_local_search", "list_itineraries"}:
                            valid &= bool(prediction.get("items"))
                        if tool == "maps_search":
                            valid &= all(item.get("id") and item.get("location") for item in prediction.get("items", []))
                        if tool == "search_music":
                            valid &= all(item.get("url") for item in prediction.get("items", []))
                        if tool == "maps_local_search":
                            valid &= bool(prediction.get("center"))
                        if tool == "maps_weather":
                            valid &= bool(prediction.get("day_weather") and prediction.get("source"))
                        if tool == "maps_route":
                            valid &= prediction.get("distance_m", 0) > 0 and prediction.get("duration_s", 0) > 0
                        if tool == "maps_place_detail":
                            valid &= prediction.get("item", {}).get("id") == arguments["poi_id"]
                        if tool == "save_itinerary":
                            valid &= prediction.get("status") == "saved" and bool(prediction.get("item_id"))
                        row.update(passed=bool(valid), prediction=prediction, metrics={"real_tool_evidence_pass": [int(bool(valid)), 1]})
                    except Exception as exc:
                        row.update(passed=False, error_type=type(exc).__name__, error=str(exc)[:1200], metrics={"real_tool_evidence_pass": [0, 1]})
                    row["elapsed_ms"] = round((perf_counter() - start) * 1000)
                    persist(row)
                    # The provider quota is separate from tool latency. Do not count pacing as execution time.
                    await asyncio.sleep(args.tool_interval)
            finally:
                await client.close()
    report = summarize(rows, cases, metadata)
    print(json.dumps({"output": str(output), "executed": report["executed"], "passed": report["passed"]}, ensure_ascii=False))
    return report["executed"] == len(cases) and report["passed"] == len(cases)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="evaluations/data/lifeplus-v1.jsonl")
    parser.add_argument("--groups", default="all")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--output")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--tool-interval", type=float, default=0.4)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be in [1,8]")
    if args.tool_interval < 0:
        parser.error("--tool-interval must not be negative")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
