"""Export portable Jev cases and evidence without private databases or new API calls."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

from .jev_ab import summarize, usage_summary

ARCHIVE = Path("evaluations/jev/2026-10-29")
LAYOUT = {
    "domain": ("modules/domain", "单任务领域仲裁", "领域准确率"),
    "cardinality": ("modules/cardinality", "单/多目标判断", "数量分类正确率"),
    "events_dev": ("modules/events/development", "事件审核开发集", "完整事件契约通过率"),
    "events_holdout": ("modules/events/holdout", "事件审核扩展留出集", "完整事件契约通过率"),
    "global": ("end_to_end/front_combined", "前序联合方案全链路", "会话全部目标完成率"),
    "boundaries": ("end_to_end/front_boundaries", "联合方案针对性链路", "会话全部目标完成率"),
}
RESULT_FIELDS = {
    "id", "repeat", "arm", "group", "query", "context", "expected_label", "expected_domain",
    "actual_label", "actual_domain", "passed", "elapsed_ms", "fallback", "source", "decision", "grade",
    "error_type", "prechecks", "precheck_counts", "attempts", "goals_total", "goals_completed", "completed",
    "failures", "goal_checks", "all_roots_completed", "root_statuses", "leaf_statuses",
}
MANIFEST_FIELDS = {
    "scope", "strategy", "started_at", "split", "repeats", "seed", "baseline_model", "candidate_model",
    "model", "confidence_threshold", "probability_threshold", "jev_timeout_seconds", "case_timeout_seconds",
    "event_timeout_seconds", "timeout", "min_confidence", "min_probability", "dataset_sha256", "sources",
    "candidate", "case_budget_seconds", "concurrency", "order", "auto_enable", "limitations", "note",
}


def relative_change(before, after):
    return (after - before) / before * 100 if before not in (None, 0) and after is not None else None


def metric_changes(summary):
    a, b = summary["arms"]["baseline"], summary["arms"]["candidate"]
    return {
        "accuracy_change_percentage_points": (b["accuracy_or_completion_rate"] - a["accuracy_or_completion_rate"]) * 100,
        "p50_change_ms": b["p50_ms"] - a["p50_ms"],
        "p50_change_percent": relative_change(a["p50_ms"], b["p50_ms"]),
        "p95_change_ms": b["p95_ms"] - a["p95_ms"],
        "p95_change_percent": relative_change(a["p95_ms"], b["p95_ms"]),
        "total_tokens_change": b["total_tokens"] - a["total_tokens"]
            if a["total_tokens"] is not None and b["total_tokens"] is not None else None,
        "total_tokens_change_percent": relative_change(a["total_tokens"], b["total_tokens"]),
        "meaning": "Positive latency/token changes mean more time/consumption; positive accuracy points mean better quality.",
    }


def validate_pairs(cases, rows, expected_count):
    ids = [case["id"] for case in cases]
    if len(ids) != expected_count or len(set(ids)) != len(ids):
        raise ValueError("Incorrect or duplicate frozen cases")
    for arm in ("baseline", "candidate"):
        selected = [r for r in rows if r["arm"] == arm]
        if len(selected) != expected_count or {r["id"] for r in selected} != set(ids):
            raise ValueError("Missing, duplicate or unmatched paired observations")
    if len(rows) != expected_count * 2:
        raise ValueError("Unexpected experiment arm")


def frozen_cases(key, manifest):
    if key not in {"global", "boundaries"}:
        return manifest["cases"]
    dataset = Path("evaluations/data") / ("jev-e2e-v1.jsonl" if key == "global" else "jev-front-boundaries-v1.jsonl")
    content = dataset.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest["dataset_sha256"]:
        raise ValueError("End-to-end source dataset no longer matches the original run")
    selected = set(manifest["cases"])
    return [case for line in content.decode().splitlines() if (case := json.loads(line))["id"] in selected]


def csv_details(cases, rows):
    by_id = {case["id"]: case for case in cases}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["id", "arm", "query", "expected", "actual", "passed",
        "elapsed_ms", "fallback", "total_tokens", "known_tokens_lower_bound", "attempts_missing_usage", "errors"])
    writer.writeheader()
    for row in rows:
        case = by_id[row["id"]]
        usage = usage_summary(row.get("attempts", []))
        writer.writerow({"id": row["id"], "arm": row["arm"], "query": case["query"],
            "expected": json.dumps(case.get("expected_label", case.get("expected_domain", case.get("expected", case.get("goals")))), ensure_ascii=False),
            "actual": json.dumps(row.get("decision", row.get("actual_label", row.get("actual_domain", row.get("goal_checks")))), ensure_ascii=False),
            "passed": row["passed"], "elapsed_ms": row["elapsed_ms"], "fallback": row.get("fallback", False),
            "total_tokens": usage["total_tokens"] if usage["total_tokens"] is not None else "unknown",
            "known_tokens_lower_bound": usage["observed_total_tokens"], "attempts_missing_usage": usage["attempts_missing_usage"],
            "errors": json.dumps(row.get("grade", {}).get("errors", row.get("failures", row.get("error_type", []))), ensure_ascii=False)})
    return output.getvalue()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def json_lines(values):
    return "".join(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n" for value in values)


def export_archive(root=ARCHIVE):
    from .jev_front_report import ROOT, RUNS

    root = Path(root)
    outputs, index = {}, []
    for key, (run_name, count) in RUNS.items():
        source = ROOT / run_name
        relative, label, metric = LAYOUT[key]
        if (source / "results.jsonl").exists():
            manifest = json.loads((source / "manifest.json").read_text())
            cases = frozen_cases(key, manifest)
            rows = [{field: value for field, value in json.loads(line).items() if field in RESULT_FIELDS}
                    for line in (source / "results.jsonl").read_text().splitlines()]
            public_manifest = {field: value for field, value in manifest.items() if field in MANIFEST_FIELDS}
            public_manifest.update(source_run=str(source), record_scope=key, metric=metric,
                frozen_case_count=count, paired_observations=count * 2,
                model_names=sorted({a["model_name"] for row in rows for a in row.get("attempts", []) if a.get("model_name")}),
                privacy="Auth, owner, top-level audit/task IDs, trace paths, logs and databases are excluded; synthetic event target IDs and model span IDs are retained.")
        else:
            previous = ARCHIVE / relative if (ARCHIVE / relative / "results.jsonl").exists() else root / relative
            cases = [json.loads(line) for line in (previous / "cases.jsonl").read_text().splitlines()]
            rows = [json.loads(line) for line in (previous / "results.jsonl").read_text().splitlines()]
            public_manifest = json.loads((previous / "manifest.json").read_text())
        validate_pairs(cases, rows, count)
        summary = summarize(rows)
        comparison = {"metric": metric, **metric_changes(summary)}
        if key == "events_dev":
            comparison["requires_model_subset"] = summarize([r for r in rows if r["group"] == "requires_model"])
        for filename, value in {"cases.jsonl": json_lines(cases), "results.jsonl": json_lines(rows),
            "manifest.json": json_text(public_manifest), "summary.json": json_text(summary),
            "comparison.json": json_text(comparison), "details.csv": csv_details(cases, rows)}.items():
            path = root / relative / filename
            if filename in {"cases.jsonl", "results.jsonl"} and path.exists() and path.read_text() != value:
                raise ValueError("Frozen cases/results changed; export to a new version directory: " + str(path))
            outputs[path] = value
        index.append({"key": key, "label": label, "directory": relative, "metric": metric, "cases": count,
            "paired_observations": count * 2, "source_run": str(source), **metric_changes(summary)})
    outputs[root / "index.json"] = json_text({"date": "2026-10-29", "datasets": index,
        "warning": "Do not pool datasets or label combined-front E2E as domain-only E2E."})
    lines = ["# Jev 测试数据与证据归档：2026-10-29", "",
        "统一测试记录：[jev-front-ab-2026-10-29.md](../../../docs/jev-front-ab-2026-10-29.md)。本目录汇集该文档六组试验的冻结Query、期望、逐条结果和汇总；不运行模型，不改变当前服务。", "",
        "| 范围 | 数据目录 | 场景数 | A/B观察数 | 质量口径 |", "|---|---|---:|---:|---|"]
    lines += [f"| {entry['label']} | [{entry['directory']}]({entry['directory']}) | {entry['cases']} | {entry['paired_observations']} | {entry['metric']} |" for entry in index]
    lines += ["", "## 每组文件", "",
        "- `cases.jsonl`：当时冻结的输入、上下文或等待任务、期望与业务目标，不从当前代码猜测旧标签。",
        "- `results.jsonl`：baseline/candidate逐条结果，保留失败、超时、回退、模型attempt、供应商usage；移除归属与运行路径。",
        "- `details.csv`：便于筛选的逐条对照表，包含原Query、期望、实际、通过、耗时、Token与错误。",
        "- `summary.json`：分子分母、正确率/完成率、nearest-rank P50/P95、分阶段用量与混淆矩阵。",
        "- `comparison.json`：准确率变化（百分点）、耗时及Token绝对/相对变化。耗时/Token正数代表增加，不自动称为收益。",
        "- `manifest.json`：原试验范围、日期、模型/门槛及来源；不含私有Key、数据库或HTTP认证。", "",
        "## 数据边界", "",
        "这六组共109个组内场景、218条A/B观察，存在场景/模板复用，不能称为109个独立用户或混算总体正确率。事件开发的17条需模型子集从30条原集派生，不重复存储或计数。",
        "领域21条的最终准确率包含Jev不接受时的LLM回退，不等同于Jev独立分类准确率。总体试验是前序联合方案，不是当前单独启用领域Jev版本的整体配对证据；针对性4例与普通12例分开。",
        "未知usage保留null，CSV记unknown，已知消耗仅为下界。原SQLite、完整Trace和日志仍保留在被Git忽略的evaluations/runs；本目录是可共享、可复算的导出，不移动或删除原始记录。", "",
        "## 维护方式", "", "```bash", ".venv/bin/python -m evaluations.jev_archive",
        ".venv/bin/python -m evaluations.jev_front_report", "```", "",
        "上述命令只处理现有文件，不产生模型费用。Query和逐条结果冻结，内容变化时拒绝覆盖，须建立新的日期/版本目录；可修复汇总算法后重建派生文件。原run目录不存在时可直接使用本归档复算。",
        "以后新实验先冻结数据和对照范围，运行后追加新版本，记录模型、配置、代码版本、统计单位、失败与未知用量，不改旧标签迎合模型输出。其他历史试验仍以旧报告及原run目录为准，不静默合并进本轮。"]
    outputs[root / "README.md"] = "\n".join(lines) + "\n"
    for path, value in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8", newline="")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ARCHIVE)
    args = parser.parse_args()
    index = export_archive(args.output)
    print(json.dumps({"archive": str(args.output), "datasets": len(index), "cases": sum(e["cases"] for e in index), "model_calls": 0}))


if __name__ == "__main__":
    main()
