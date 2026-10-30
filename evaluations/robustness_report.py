"""Append immutable post-fix E2E and robustness evidence without mixing scopes."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .current_measurements import aggregate, clean_completion, frozen

RUN = Path("evaluations/runs/robustness30-20261030-03")
E2E = Path("evaluations/runs/post-binding-completion90-20261030-01")
RAG = Path("evaluations/runs/robustness30-20261030-02/rag-container.json")
PUBLIC = Path("测试数据集/结果记录/2026-10-30-robustness-01")
OLD_FAILURES = [f"multi_completion-{i:02}" for i in (4, 8, 12, 16, 20, 28, 29, 30)]


def assertions(path):
    return [{"id": test.get("classname") + "::" + test.get("name"),
             "status": "blocked" if test.find("skipped") is not None else "failed" if any(test.find(k) is not None for k in ("failure", "error")) else "passed",
             "elapsed_ms": round(float(test.get("time", 0)) * 1000)} for test in ET.parse(path).findall(".//testcase")]


def load(output, root):
    target, run, e2e = output / PUBLIC, root / RUN, root / E2E
    summary_path = target / "汇总.json"
    if (run / "summary.json").exists() and (e2e / "summary.json").exists() and (root / RAG).exists():
        tail = json.loads((run / "summary.json").read_text())
        if not tail["complete"] or json.loads((e2e / "summary.json").read_text())["tested"] != 90:
            return None
        rows = clean_completion(e2e)
        tails = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
        value = {"model": "deepseek-flash", "thinking": "disabled", "full90": aggregate(rows),
            "full90_groups": {g: aggregate([row for row in rows if row["group"] == g]) for g in sorted({row["group"] for row in rows})},
            "old8": aggregate([row for row in rows if row["id"] in OLD_FAILURES]),
            "old8_ids": OLD_FAILURES, "tail30": tail,
            "structured_false_completion": {"numerator": sum(row.get("all_roots_completed", False) and not row["completed"] for row in rows),
                                            "denominator": sum(row.get("all_roots_completed", False) for row in rows)},
            "rag": json.loads((root / RAG).read_text()),
            "supplemental": json.loads((run / "supplemental-contracts.json").read_text()),
            "contract_assertions": assertions(run / "contracts.xml")}
        frozen(target / "完整90会话逐条结果.json", rows)
        frozen(target / "完整90会话运行条件.json", json.loads((e2e / "manifest.json").read_text()))
        frozen(target / "长尾30条逐条结果.json", tails)
        frozen(target / "长尾30条运行条件.json", json.loads((run / "manifest.json").read_text()))
        frozen(target / "RAG容器复验.json", value["rag"])
        frozen(target / "故障断言明细.json", value["contract_assertions"])
        # Failed instrumentation is retained, but is not counted as a product defect.
        for version in ("01", "02"):
            old = root / f"evaluations/runs/robustness30-20261030-{version}"
            frozen(target / f"夹具诊断{version}.json", {"manifest": json.loads((old / "manifest.json").read_text()),
                "results": [json.loads(line) for line in (old / "results.jsonl").read_text().splitlines()],
                "assertions": assertions(old / "contracts.xml"),
                "note": "attempt列名、父图/领域状态及缺任务行属于测试夹具问题；本机Chroma不可用另作容器复验；模型17条复用未重跑"})
        frozen(summary_path, value)
    else:
        retained = root / "测试回报" / PUBLIC
        source = retained if (retained / "汇总.json").exists() else target
        if not (source / "汇总.json").exists():
            return None
        value = json.loads((source / "汇总.json").read_text())
        for file in source.glob("*.json"):
            frozen(target / file.name, json.loads(file.read_text()))
    validation_path = target / "报告维护验证.json"
    checks = [root / "data" / name for name in ("robustness-report-tests-20261030.xml", "robustness-report-tests-20261030-02.xml")]
    if all(path.exists() for path in checks):
        checks_value = {"paid_model_calls": 0, "scope": "isolated_engineering_snapshot_not_model_or_business_accuracy",
            "reason": "First regression exposed report-only fallback trusting a changed destination; prefer retained canonical evidence and rerun without private dotenv",
            "runs": [{"source": str(path.relative_to(root)), "counts": dict(Counter(item["status"] for item in assertions(path))),
                      "failed_ids": [item["id"] for item in assertions(path) if item["status"] == "failed"]} for path in checks]}
        frozen(validation_path, checks_value)
    if validation_path.exists():
        value = {**value, "report_validation": json.loads(validation_path.read_text())}
    (target / "README.md").write_text(
        "# 修复后完整会话与长尾复验\n\n真实模型关闭思考、顺序运行，未改业务逻辑、提示词、正常配置或部署。"
        "完整90会话复用D06，与修复前为历史顺序比较，不是新随机A/B；另17条模型组件与13条受控契约分开统计。"
        "旧8条包含于90条，不相加算98条；17条模型观察仅请求一次，在夹具复跑中复用。\n\n"
        "- 完整90会话逐条结果/运行条件：真实handler、模型、Graph、MCP、SQLite，自动选择/确认，禁用评测默认定位匹配gold。\n"
        "- 长尾30条逐条结果/运行条件：输入见D18；事件5、规划11、本域1及受控13场景，覆盖边界逐条声明。\n"
        "- 故障断言明细：参数化工程断言与场景数分开，本机缺Chroma为blocked；SSE/重启为受控测试。\n"
        "- RAG容器复验：当前rag.py源码hash一致，容器内全新临时目录，真实Chroma，不触碰正常索引。\n"
        "- 夹具诊断01/02：保留初始测试错误与复跑，不能把修正测试夹具说成修复业务缺陷；不导出原始stdout/私有目录/数据库。\n"
        "- 报告维护验证.json（若存在）：本轮工程隔离回归、归档工具失败及修正后复跑；不计模型样本。\n"
        "- 汇总.json：分层指标与失败清单，未知Token不计零。模型局部通过不能代替端到端完成率，单次90/90不等于生产100%。\n")
    return value


def tokens(value):
    return str(value["total_tokens"]) if value["total_tokens"] is not None else f"≥{value['observed_total_tokens']}（{value['attempts_missing_usage']}次未知）"


def section(value):
    if not value:
        return ""
    full = value["full90"]
    lines = ["## 0.2 修复后完整会话、长尾与故障复验", "",
        "本轮仅新增数据、评测及报告，业务代码、提示词和思考配置均未修改。真实模型实验串行，不与其他付费实验重叠；不重启或修改正常服务。", "",
        "### 完整90会话与原失败复验", "",
        "| 范围 | 完整通过/提交 | P50秒 | P95秒 | Token总量 |", "|---|---|---|---|---|"]
    for name, item in [("完整90会话", full), *value["full90_groups"].items(), ("其中：原8个失败", value["old8"])]:
        lines.append(f"| {name} | {item['passed']}/{item['submitted']} | {item['p50_ms']/1000:.3f} | {item['p95_ms']/1000:.3f} | {tokens(item)} |")
    false = value["structured_false_completion"]
    lines += ["", f"目标级：{full['goals_completed']}/{full['goals_total']}；结构化虚假完成代理指标：{false['numerator']}/{false['denominator']}。"
        "原8条均在90条内，不能另外增加样本量。五条天气保存和三条同域遗漏会话本次均通过实际回执、写入授权和目标检查。", "",
        "修复前82/90→本次90/90是同冻结集的历史回归比较，不是随机配对A/B或线上100%保证。"
        "修复前P50/P95为4.132/6.551秒，本次4.380/5.810秒，中位耗时没有改善；两轮Token都有未知用量，不能声称精确节省。"
        "仍为单并发、自动用户回复的ASGI测试，不包含浏览器/TCP、人类等待、天气事实性核对或生产压测。", "",
        "### 新增30条按实际验证层级分开", "",
        "| 验证层 | 通过/提交 | P50秒 | P95秒 | Token总量 |", "|---|---|---|---|---|"]
    labels = {"event_live": "真实事件理解", "planner_live": "真实规划", "domain_live": "真实领域参数理解", "controlled_contract": "受控故障/协议场景"}
    for layer, item in value["tail30"]["by_layer"].items():
        lines.append(f"| {labels[layer]} | {item['passed']}/{item['submitted']} | {item['p50_ms']/1000:.3f} | {item['p95_ms']/1000:.3f} | {tokens(item) if layer != 'controlled_contract' else '无模型调用，不计入模型成本对比'} |")
    lines += ["", "30条不是30个真实模型端到端会话；受控测试时间是断言执行时间，不可与会话延迟相比。"
        "17条模型观察只执行一次，后续两次是修正评测夹具后的受控复跑，不重复算模型样本。", "",
        "### 该轮失败与边界（后续修复另列）", "",
        "- **tail-A01：** 唯一待处理任务仍反问取消哪个；与旧boundary04一致，事件语义问题尚未修复。",
        "- **tail-A03：** 引用‘第二个’询问含义仍被当成候选操作；与旧boundary06一致。",
        "- **tail-C03：** 三层依赖计划把destination绑定到整个selected对象，并添加t1.slots.origin等未经证明存在的来源。"
        "语法根合法不代表接收类型和字段存在；本轮只测规划，未声称实际导航误执行。",
        "- **tail-D05：** 绑定层直接读取过期天气回执，未拒绝或触发刷新。已确认的是_bind层时效门禁缺口，"
        "尚未证明过期内容一定落库；仍需下游确认/写入端到端验证。",
        "- **RAG删除/缩短：** 现有容器真实Chroma两例仍失败，删除后2→2（应0）、缩短后2→2（应1），旧文本仍可召回。"
        "受测rag.py与当前源码hash一致，未修改正常知识库。", "",
        "### 测试本身的诊断与恢复", "",
        "初轮发现测试把tool_jobs.attempt写成attempts、在父图而非领域authorize节点修改确认参数、以及未创建task行就要求持久化重试Trace。"
        "这些是夹具错误，已修正并保留原始失败，不能计作三个生产缺陷。本机未安装Chroma的两例另记blocked，随后在现有容器临时索引完成真实审计。"
        "本轮没有因此改业务实现或提示词，也没有重跑挑选17条模型的成功输出。", "",
        "SSE断线游标续传、大于单页事件重放及独立等待分支重启恢复共3项受控检查通过；"
        "不代表实测公网断网或多实例切换。写后丢回执、并发确认、重复提交、业务失败及慢分支隔离通过对应受控检查。", "",
        f"[逐条结果、运行条件及夹具失败记录]({PUBLIC.as_posix()}/README.md) · "
        "[30条输入、前提与断言](测试数据集/13-长尾与故障/长尾与故障30条-v1.jsonl)。", ""]
    if value.get("report_validation"):
        before, after = (item["counts"] for item in value["report_validation"]["runs"])
        lines += [f"本轮隔离工程回归首次{before.get('passed', 0)}通过、{before.get('failed', 0)}失败、{before.get('blocked', 0)}跳过；"
            "发现的是报告重建对已改动目标副本的信任问题，已修正证据读取顺序。"
            f"复跑{after.get('passed', 0)}通过、{after.get('failed', 0)}失败、{after.get('blocked', 0)}跳过。两次均保留，未改业务实现或模型成绩。", ""]
    return "\n".join(lines)
