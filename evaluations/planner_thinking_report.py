"""Offline archive and handbook section for planner-only thinking experiments."""
from __future__ import annotations

import json
from pathlib import Path

RUN = Path("evaluations/runs/planner-thinking-20261030-01")
PUBLIC = Path("测试数据集/结果记录/2026-10-30-planner-thinking-01")


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def frozen(path, data):
    if path.exists() and path.read_bytes() != data:
        raise ValueError("Frozen thinking experiment differs; use a new version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load(output, root):
    source = root / RUN
    target = output / PUBLIC
    if (source / "summary.json").exists():
        result = json.loads((source / "summary.json").read_text())
        if not result["complete"] or result["submitted"] != 99:
            return None
        rows = read_rows(source / "results.jsonl")
        cases = read_rows(source / "cases.jsonl")
        if len(rows) != 99 or len({(row["id"], row["arm"]) for row in rows}) != 99:
            raise ValueError("Missing or duplicated experimental submissions")
        if result["manifest"]["model"] != "deepseek-flash":
            raise ValueError("Wrong experimental model")
        binding_ids = {item["id"] for item in cases if item["scope"] == "main30"
                       and any(spec["binds"] for spec in item["tasks"])}
        result["binding_cases"] = {arm: {"submitted": sum(row["id"] in binding_ids and row["arm"] == arm for row in rows),
            "passed": sum(row["id"] in binding_ids and row["arm"] == arm and row["passed"] for row in rows)}
            for arm in ("disabled", "low", "high")}
        for original, name in (("manifest.json", "运行条件.json"), ("results.jsonl", "逐条结果.jsonl"), ("cases.jsonl", "规划思考模式33条.jsonl")):
            frozen(target / name, (source / original).read_bytes())
        frozen(target / "实测汇总.json", (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    else:
        retained = target if (target / "实测汇总.json").exists() else root / "测试回报" / PUBLIC
        if not (retained / "实测汇总.json").exists():
            return None
        result = json.loads((retained / "实测汇总.json").read_text())
        for name in ("运行条件.json", "逐条结果.jsonl", "规划思考模式33条.jsonl", "实测汇总.json"):
            frozen(target / name, (retained / name).read_bytes())
    (target / "README.md").write_text(
        "# 只为全局规划开启思考：关闭／low／high\n\n"
        "真实DeepSeek Flash，30条原规划回归集＋3条同域目标合并诊断（后验案例，独立统计），每条各请求三种模式。"
        "单并发、打乱场景、六种调用次序轮换；同一原提示词、JSON契约、固定参考时间、校验及一次修复。"
        "只创建实验规划客户端，不修改.env、领域/回复模型、服务或Docker，不调用业务工具。\n\n"
        "- 规划思考模式33条.jsonl与同名说明：原始输入、领域/依赖/绑定期望及scope。\n"
        "- 逐条结果.jsonl：最终JSON、评分、异常、修复尝试、finish_reason和usage；不保存隐藏思考文本。\n"
        "- 运行条件.json：实际Token上限、25秒请求超时、45秒累计规划预算、模型和源码hash。\n"
        "- 实测汇总.json：30条主结果、3条诊断、配对修复与退化、推理及总Token；reasoning不重复相加。\n\n"
        "本次是一轮回归，不是盲测或全链路任务完成率；缓存/供应商负载不受控，thinking模式下temperature不保证生效。"
        "参数以[DeepSeek官方文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)为依据。\n")
    (target / "规划思考模式33条.说明.md").write_text(
        "# 数据集用途\n\nmain30复用D04原30条：单目标标点、并行、依赖、缺参、上下文、边界各5。"
        "diagnostic3来自已知‘天气＋提醒＋行程’失败表达，用不同城市验证同领域独立目标是否合并，不是未见留出集。"
        "任务ID可重命名，独立任务顺序可交换；按grade_goals检查数量、领域、查询关键词、依赖和来源绑定，"
        "不检查已移到领域层的工具参数，也未对所有自然语言约束作人工审核。固定时间2026-10-27T10:00:00+08:00。\n")
    (target / "逐条结果.说明.md").write_text(
        "# 逐条观察用途\n\n每行一个场景的一种模式；33个场景×3模式共99观察，不是99个独立场景。"
        "passed要求原有计划评分通过，异常与截断均保留；attempts包含失败修复用量、最终JSON及校验摘要。"
        "P50/P95包含失败，按nearest-rank。仅局部规划时延，不是端到端时延。\n")
    return result


def tokens(item):
    return str(item["total_tokens"]) if item["total_tokens"] is not None else (
        f"≥{item['observed_total_tokens']}（{item['attempts_missing_usage']}次未知）")


def section(result):
    manifest = result["manifest"]
    lines = ["## 0.1 规划器单独开启思考的真实对比", "",
        "**仅测试，不自动启用。** 只改变实验全局规划器的思考开关/强度，事件、领域、槽位及回复服务配置保持不变。"
        "同一轮重跑关闭思考作为对照，不把之前23/30直接拼成新配对。", "",
        "| 模式 | 30条严格通过 | 绑定场景通过 | P50秒 | P95秒 | 总Token | 已报告推理Token | 修复次数 | 截断attempt |",
        "|---|---|---|---|---|---|---|---|---|"]
    for arm, name in (("disabled", "关闭思考"), ("low", "开启low"), ("high", "开启high")):
        item, binding = result["scopes"]["main30"]["arms"][arm], result["binding_cases"][arm]
        lines.append(f"| {name} | {item['passed']}/{item['submitted']} | {binding['passed']}/{binding['submitted']} | "
            f"{item['p50_ms']/1000:.3f} | {item['p95_ms']/1000:.3f} | {tokens(item)} | "
            f"{item['reported_reasoning_tokens']} | {item['repairs']} | {item['length_attempts']} |")
    lines += ["", "### 配对效果", ""]
    main = result["scopes"]["main30"]
    a = main["arms"]["disabled"]
    for arm in ("low", "high"):
        item, paired = main["arms"][arm], main["paired"][arm]
        delta = (item["passed"] - a["passed"]) / a["submitted"] * 100
        lines.append(f"- {arm}：严格通过率变化{delta:+.2f}个百分点；救回{len(paired['rescued'])}条、"
            f"新增失败{len(paired['regressed'])}条。救回ID：{', '.join(paired['rescued']) or '无'}；新增失败：{', '.join(paired['regressed']) or '无'}。")
        if item["total_tokens"] is not None and a["total_tokens"]:
            lines.append(f"- {arm}代价：P50增加{(item['p50_ms']/a['p50_ms']-1):.2%}，P95增加{(item['p95_ms']/a['p95_ms']-1):.2%}，"
                f"总Token增加{(item['total_tokens']/a['total_tokens']-1):.2%}。每成功规划摊销Token "
                f"{a['total_tokens']/a['passed']:.1f}→{item['total_tokens']/item['passed']:.1f}（全部失败消耗也计入分子，不是业务目标成本）。")
    lines += ["", "### 同领域目标合并诊断（另外3条，不混入30条）", "",
        "| 模式 | 全部三目标正确拆分 | P50秒 | P95秒 | 总Token |", "|---|---|---|---|---|"]
    for arm in ("disabled", "low", "high"):
        item = result["scopes"]["diagnostic3"]["arms"][arm]
        lines.append(f"| {arm} | {item['passed']}/{item['submitted']} | {item['p50_ms']/1000:.3f} | {item['p95_ms']/1000:.3f} | {tokens(item)} |")
    diagnostic = result["scopes"]["diagnostic3"]
    lines += ["", "### 结果解读", "",
        "- low在原回归集比high救回更多绑定案例，但两者都没有根治契约问题。应优先把合法目标字段暴露给模型，并提供精确校验反馈，而不是用更多思考替代业务协议。",
        "- 三组均无输出截断；失败仍存在非法绑定目标，不能归因输出上限过小，因此本轮不另加扩额试验。",
        f"- 同领域诊断low救回{len(diagnostic['paired']['low']['rescued'])}条、同时退化{len(diagnostic['paired']['low']['regressed'])}条；"
        f"high退化{len(diagnostic['paired']['high']['regressed'])}条。总通过数不能遮住案例级退化，同领域任务拆分仍需明确原子目标规则。",
        "- 主回归一轮中low较值得继续验证，但不建议直接全量启用high，也没有自动切换low。下一步应在契约修复后重新比较关闭思考与low，再验证复杂规划按需开启思考的策略；当前还不能声称端到端完成率提高。"]
    lines += ["", "### 实验边界与复现", "",
        f"- 实际输出上限三组均{manifest['effective_max_tokens']}，单次超时{manifest['request_timeout_seconds']}秒，累计规划预算{manifest['planning_deadline_seconds']}秒，SDK重试0，结构修复最多一次。思考Token也会消耗输出预算。",
        "- 当前baseline的实际规划客户端继承MODEL_MAX_OUTPUT_TOKENS=2048；配置中的PLANNER_MAX_OUTPUT_TOKENS=1536未单独创建客户端。本轮记录有效值，不顺手修改该行为。",
        "- 使用原提示词与GoalPlan，未增加合法绑定字段提示，也未修改错误修复信息；可以观察思考本身是否改善，不能把工程契约修复混进结果。",
        "- 随机场景顺序、六种模式次序轮换、单并发；一轮小样本，缓存、供应商负载及思考采样不完全可控，不能用单例或偶然快慢认定high必然优于low。",
        "- 30条是已查看回归集，3条为后验诊断，不证明未见泛化。评分检查目标/领域/依赖/绑定，不代表所有自然语言限制、工具执行或完整会话都通过。",
        "- 推理Token已包含在供应商total中，不能再相加；未知usage不计0。只保存最终JSON和用量，不保存隐藏思考文本。",
        f"- [本次数据、原始最终JSON、用量与运行条件]({PUBLIC.as_posix()}/README.md)；[DeepSeek参数语义]({manifest['thinking_doc']})。",
        "- 尚未测量：只给真实业务全链路的planner角色开启思考后的任务完成率、端到端P95和成本；局部改善不直接当全局改善。", ""]
    return "\n".join(lines)
