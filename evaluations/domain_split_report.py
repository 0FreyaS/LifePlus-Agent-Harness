"""Publish only counts and sanitized case outcomes from the domain-split pilot."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

from evaluations.run import percentile


def summarize(rows):
    errors = Counter(row.get("error") for row in rows if row.get("error"))
    latencies = [row["elapsed_ms"] for row in rows]
    return {"tested": len(rows), "passed": sum(row["grade"]["passed"] for row in rows),
            "error_types": dict(errors), "p50_ms_including_failures": statistics.median(latencies),
            "p95_ms_including_failures": percentile(latencies, .95),
            "attempts": sum(len(row["attempts"]) for row in rows),
            "cases": [{"id": row["id"], "passed": row["grade"]["passed"], "errors": row["grade"]["errors"],
                       "elapsed_ms": row["elapsed_ms"]} for row in rows]}


def run(args):
    data = args.data
    arms = {arm: summarize(json.loads((data / f"{arm}.json").read_text())) for arm in ("baseline", "candidate")}
    root = ElementTree.parse(data / "backend.xml").getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    backend = {key: sum(int(suite.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
    backend["passed"] = backend["tests"] - backend["failures"] - backend["errors"] - backend["skipped"]
    tools = json.loads((data / "real-tools/summary.json").read_text())["groups"]["real_tools"]
    result = {"date": "2026-10-29", "model": "grok-4.3", "arms": arms, "backend": backend,
              "real_mcp": {"passed": tools["passed"], "total": tools["executed"]}, "frontend": {"passed": 43, "skipped": 1, "build": "passed"},
              "comparison_valid": False, "improvement_proven": False,
              "conditions": {"reference_time": "2026-10-27T10:00:00+08:00", "cases_per_arm": 16,
                             "case_timeout_seconds": 120, "provider_timeout_seconds": 25, "provider_retries": 0,
                             "scope": "Planning and domain parameter readiness; no business tools, no response model, dependency layers sequential.",
                             "case_order": "Same order, each arm serial; arm run windows overlapped. Not balanced randomized AB/BA."},
              "limitations": ["Pilot candidate preceded final domain wording and checkpoint-refresh fixes; not a final production acceptance.",
                              "Provider timeouts/429 and two candidate parser failures remain in denominators.",
                              "Readiness latency is not end-to-end P95; subset successes do not establish improved accuracy.",
                              "No new real-model plus tool end-to-end success claim; controlled integrations and real MCP are separate evidence."],
              "final_source_sha256": {name: hashlib.sha256((Path("src/car_agent") / name).read_bytes()).hexdigest()
                                      for name in ("task_planning.py", "harness.py", "domain_understanding.py", "orchestration.py")}}
    target = Path("docs/assets/domain-execution-20261029.json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    a, b = arms["baseline"], arms["candidate"]
    report = f"""# 领域提参改造与测试结果

日期：2026-10-29。实现说明见 [领域执行设计](domain-execution-routing.md)，可复算计数见 [公开摘要](assets/domain-execution-20261029.json)。

## 当前结论

职责调整已完成，并通过受控回归；本轮不能证明真实模型准确率或响应速度提高。真实比较受到超时、HTTP 429 和结构化输出失败影响，新版原始通过数低于旧版，不能把失败剔除后宣称优化成功。

真实模型 pilot 为开发中的中间版本，之后补充了领域含义说明、候选刷新参数保留及提供商错误别名恢复。最终代码重新完成本地回归，但没有伪称又完成一轮有效真实模型比较。

## 已验证的实现行为

- 全局 GoalPlan 禁止 action/slots；领域子图 understand 才选择候选工具和提取领域字段。
- 多任务子图可以并发进行领域理解，前置成功后才启动依赖任务并传递实际回执。
- 模型生成的替代 details 不能覆盖前置真实天气；用户确认仍在写操作之前。
- 越域工具、动作工具不匹配、越域槽位会在工具入队前拒绝，测试检查零 tool job。
- 候选刷新保留此前澄清的起点；原中断恢复不重新规划。
- 候选数量较少时提供该领域全部受限候选，尚无数百工具向量检索效果数据。

## 最终代码回归

| 检查 | 结果 | 证据边界 |
|---|---:|---|
| 后端 | {backend['passed']} 通过，{backend['skipped']} 跳过 | 包含图、API、状态隔离、参数、恢复和故障契约；不是模型准确率 |
| 前端 | 43 通过，1 跳过；构建通过 | 新 understand 节点展示及现有交互回归 |
| 真实 MCP | {tools['passed']}/{tools['executed']} | 隔离工具数据库；真实地图、音乐、行程保存和列表；不含模型选工具 |

## 真实模型前后 pilot

同一配置模型 grok-4.3，16 条手工标注案例：5 单任务、4 并行、2 依赖、1 上下文、4 分隔边界。日期固定为 2026-10-27 北京时间。每组场景串行，领域无依赖调用并发；两组执行时间有重叠，没有随机 AB/BA，因此不作统计因果结论。

比较从规划入口到领域参数就绪，不调用业务工具和回复模型；依赖按拓扑层推进。单请求供应商超时 25 秒、SDK 不重试，Schema 修复最多一次，场景预算 120 秒。任何异常都保留在总分母及耗时中。

| 原始观测 | 修改前 | 中间候选 |
|---|---:|---:|
| 严格契约通过 | {a['passed']}/{a['tested']} | {b['passed']}/{b['tested']} |
| 供应商超时 | {a['error_types'].get('OpenAITimeoutError', 0)} | {b['error_types'].get('OpenAITimeoutError', 0)} |
| HTTP 429 | {a['error_types'].get('OpenAIRateLimitError', 0)} | {b['error_types'].get('OpenAIRateLimitError', 0)} |
| 结构化解析失败 | {a['error_types'].get('OutputParserException', 0)} | {b['error_types'].get('OutputParserException', 0)} |
| 无执行异常但严格契约失败 | 3 | 1 |
| 全请求 P50 | {a['p50_ms_including_failures']/1000:.3f} 秒 | {b['p50_ms_including_failures']/1000:.3f} 秒 |
| 全请求 P95 | {a['p95_ms_including_failures']/1000:.3f} 秒 | {b['p95_ms_including_failures']/1000:.3f} 秒 |
| 模型 attempt 数 | {a['attempts']} | {b['attempts']} |

修改前语义/覆盖失败包括健康领域误判和四类分隔输入中的目标遗漏；另外两条分隔输入发生超时。候选一次将“列出提醒”归为 wellbeing，后续提示词补充了提醒和行程属于 planning，但没有凭修改提示词就宣布该案例已在真实模型中修复。候选两个 OutputParserException 是实际输出失败，必须计入质量损失，不能全部推给供应商可用性。

最小独立模型探测也收到 HTTP 429，因此没有继续发送大量注定失败的请求。没有修改用户模型配置、增加隐藏重试或切换模型来制造更好成绩。

## 复现

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m evaluations.domain_split --source . --output data/domain-split-next/candidate.json --pause 3
.venv/bin/python -m evaluations.run --groups real_tools --output data/domain-split-next/real-tools --concurrency 1
```

旧版源码可从提交 ab2a788 导出到临时目录，使用相同 domain_split 程序的 --source 指向它；真实比较需恢复供应商可用性并安排随机交替配对。全局规划独立 A/B 使用 grade_goals，不再把尚未提取的槽位算错；联合准确率使用 domain_split 的领域输出评分。原始本地记录在 data/domain-split-20261029/，含开发失败，不上传数据库、密钥或供应商原始响应。

下一轮应分别增加目标覆盖/依赖、最终工具、字段及整组参数、端到端完成率，并报告成功与失败耗时、P50/P95 和每成功任务成本。当前没有有效证据支持生产准确率或延迟提升百分比。
"""
    Path("docs/domain-execution-evaluation.md").write_text(report, encoding="utf-8")
    print(json.dumps({"backend": backend, "baseline": a["passed"], "candidate": b["passed"], "improvement_proven": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/domain-split-20261029"))
    run(parser.parse_args())
