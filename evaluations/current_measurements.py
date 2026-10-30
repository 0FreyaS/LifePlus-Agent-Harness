"""Portable, sanitized current-policy measurements for the unified handbook."""
from __future__ import annotations

import json
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .jev_ab import read_e2e_rows, usage_summary, percentile

VERSION = "2026-10-30-01"
COMPONENT_RUN = Path("evaluations/runs/deepseek-components-20261030-01")
COMPLETION_RUN = Path("evaluations/runs/deepseek-no-default-90-20261030-01")
DEFAULT_RUN = Path("evaluations/runs/deepseek-current-90-20261030-01")
PUBLIC = Path("测试数据集/结果记录") / VERSION


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def frozen(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists() and path.read_bytes() != data:
        raise ValueError("Frozen current evidence differs; use a new version: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def public_attempt(attempt):
    if attempt.get("model_name") not in {None, "deepseek-flash", "jev-1.14.0"}:
        raise ValueError("Non-baseline model in current measurements")
    return {key: attempt[key] for key in ("stage", "model_name", "attempt", "elapsed_ms", "usage",
            "usage_known", "error_type", "type") if key in attempt}


def aggregate(rows):
    attempts = [attempt for row in rows for attempt in row.get("attempts", [])]
    return {"submitted": len(rows), "passed": sum(row.get("passed", row.get("completed", False)) for row in rows),
        "goals_completed": sum(row.get("goals_completed", 0) for row in rows),
        "goals_total": sum(row.get("goals_total", 0) for row in rows),
        "p50_ms": percentile([row["elapsed_ms"] for row in rows], .5),
        "p95_ms": percentile([row["elapsed_ms"] for row in rows], .95),
        **usage_summary(attempts),
        "by_stage": {stage: usage_summary([attempt for attempt in attempts if attempt.get("stage") == stage])
            for stage in sorted({attempt.get("stage", "unknown") for attempt in attempts})}}


def clean_completion(path):
    rows = read_e2e_rows(path, "current")
    with sqlite3.connect((path / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        for row in rows:
            events = [{"type": kind, **json.loads(data)} for kind, data in db.execute(
                "SELECT e.type,e.data_json FROM events e JOIN tasks t ON e.task_id=t.id WHERE t.user_id=? ORDER BY e.created_at,e.seq",
                (row["owner"],))]
            row["diagnostics"] = [{key: event[key] for key in ("type", "answer", "error", "source", "realtime_gps", "values", "tasks") if key in event}
                for event in events if event["type"] in {"task.failed", "plan.completed", "context.location_defaulted"}]
    return [{**{key: row[key] for key in ("id", "group", "query", "completed", "passed", "goals_total",
        "goals_completed", "all_roots_completed", "root_statuses", "leaf_statuses", "goal_checks", "failures", "elapsed_ms", "diagnostics") if key in row},
        "attempts": [public_attempt(attempt) for attempt in row["attempts"]]} for row in rows]


def load(output, root):
    target = output / PUBLIC / "实测汇总.json"
    components, completion = root / COMPONENT_RUN, root / COMPLETION_RUN
    if all((path / "summary.json").exists() for path in (components, completion)):
        c = json.loads((components / "summary.json").read_text())
        e = json.loads((completion / "summary.json").read_text())
        if c["submitted"] != 120 or e["tested"] != 90:
            return None
        module_rows = read_rows(components / "results.jsonl")
        clean_modules = [{**{key: row[key] for key in ("id", "group", "query", "passed", "metrics",
            "prediction", "graph", "grade", "expected_domain", "error_type", "elapsed_ms") if key in row},
            "attempts": [public_attempt(attempt) for attempt in row["attempts"]]} for row in module_rows]
        clean_e2e = clean_completion(completion)
        default_rows = clean_completion(root / DEFAULT_RUN)
        frozen(output / PUBLIC / "组件逐条结果.json", clean_modules)
        frozen(output / PUBLIC / "全链路逐条结果.json", clean_e2e)
        frozen(output / PUBLIC / "默认定位兼容性审计90会话.json", default_rows)
        frozen(output / PUBLIC / "默认定位兼容性审计条件.json", json.loads((root / DEFAULT_RUN / "manifest.json").read_text()))
        frozen(output / PUBLIC / "组件运行条件.json", c["manifest"])
        frozen(output / PUBLIC / "全链路运行条件.json", json.loads((completion / "manifest.json").read_text()))
        false_num = sum(row.get("all_roots_completed", False) and not row["completed"] for row in clean_e2e)
        false_den = sum(row.get("all_roots_completed", False) for row in clean_e2e)
        value = {"version": VERSION, "model": "deepseek-flash", "thinking": "disabled",
            "components": c["groups"], "component_total": aggregate(clean_modules),
            "completion": {group: aggregate([row for row in clean_e2e if row["group"] == group])
                for group in sorted({row["group"] for row in clean_e2e})},
            "completion_total": aggregate(clean_e2e),
            "default_location_audit": aggregate(default_rows),
            "default_location_audit_groups": {group: aggregate([row for row in default_rows if row["group"] == group])
                for group in sorted({row["group"] for row in default_rows})},
            "defaulted_ids": [row["id"] for row in default_rows if any(event["type"] == "context.location_defaulted" for event in row["diagnostics"])],
            "structured_false_completion": {"numerator": false_num, "denominator": false_den},
            "component_failures": [row for row in clean_modules if not row["passed"]],
            "completion_failures": [row for row in clean_e2e if not row["completed"]]}
        value["experiment_consumption"] = usage_summary([attempt
            for row in [*clean_modules, *clean_e2e, *default_rows] for attempt in row["attempts"]])
        slot_path = root / "测试回报/测试数据集/02-槽位与参数/槽位提取30条.jsonl"
        slots = {row["id"]: row for row in clean_modules if row["group"] == "slot_extraction"}
        constraints = [(case["id"], field) for case in read_rows(slot_path)
                       for field, expected in case["expected"]["slots"].items() if expected is None]
        value["no_inference_constraints"] = {"numerator": sum(
            "prediction" in slots[cid] and slots[cid]["prediction"].get("slots", {}).get(field) in (None, "")
            for cid, field in constraints), "denominator": len(constraints),
            "cases": [{"id": cid, "field": field} for cid, field in constraints]}
        frozen(target, value)
    else:
        retained = target if target.exists() else root / "测试回报" / PUBLIC / "实测汇总.json"
        if not retained.exists():
            return None
        value = json.loads(retained.read_text())
        source = retained.parent
        for path in source.glob("*.json"):
            frozen(output / PUBLIC / path.name, json.loads(path.read_text()))
    (output / PUBLIC / "README.md").write_text(
        "# 当前配置真实实测（2026-10-30）\n\n"
        "组件120条：当前Jev级联领域→DeepSeek领域理解，澄清通过真实图、固定工具回执；规划强制进入全局模型，不测数量门控。"
        "完整90会话：当前llm_direct＋jev_cascade＋rules、真实模型和MCP、隔离SQLite/Chroma，关闭配置默认位置以匹配冻结标签。"
        "另外保留生产默认定位条件下90会话兼容性审计；不能混算。单并发顺序运行，没有并行运行其他付费实验。"
        "仅评测进程禁用配置默认位置，正常服务不变。\n\n"
        "组件逐条结果与全链路逐条结果保留失败、输出、评分及全部模型attempt用量。运行条件包含开始时间、日期锚点、源文件及数据hash；"
        "不含Key、完整会话库或本机数据库。单次回归不是未见集泛化、生产QPS或人工质量评分。\n\n"
        "工程回归摘要.json单列读取本地配置与无私有配置隔离副本两种测试条件，不将受控用例数算作真实会话完成率。\n")
    review_path = output / PUBLIC / "工程回归摘要.json"
    review_files = {"local_config": "deepseek-baseline-tests-20261030.xml",
                    "isolated": "deepseek-baseline-isolated-tests-20261030.xml"}
    if all((root / "data" / name).exists() for name in review_files.values()):
        review = {}
        for label, name in review_files.items():
            cases = list(ET.parse(root / "data" / name).iter("testcase"))
            failed = [case for case in cases if case.find("failure") is not None or case.find("error") is not None]
            skipped = sum(case.find("skipped") is not None for case in cases)
            review[label] = {"source": "data/" + name, "submitted": len(cases),
                "passed": len(cases) - len(failed) - skipped, "failed": len(failed), "skipped": skipped,
                "failed_ids": [case.get("classname") + "::" + case.get("name") for case in failed]}
        frozen(review_path, review)
    elif review_path.exists():
        review = json.loads(review_path.read_text())
    else:
        review = None
    return {**value, "engineering_runs": review} if review else value


def table_row(label, item):
    tokens = str(item["total_tokens"]) if item["total_tokens"] is not None else (
        f"≥{item['observed_total_tokens']}（{item['attempts_missing_usage']}次未知）")
    return f"| {label} | {item['passed']}/{item['submitted']} | {item['p50_ms']/1000:.3f} | {item['p95_ms']/1000:.3f} | {tokens} |"


def section(value):
    lines = ["## 0. 本轮当前配置真实测试", "", "实测日期：2026-10-30。以下为本轮新增实测，不是旧模型数字迁移，也不是新A/B。测试过程中没有修业务提示词或运行逻辑，没有更新正常服务配置。", "",
        "| 专项 | 完整通过/提交 | P50秒 | P95秒 | Token总量 |", "|---|---|---|---|---|"]
    for group, label in (("single_task", "领域＋动作30条"), ("slot_extraction", "槽位30条"),
        ("clarification", "澄清正负例30条"), ("planner", "全局规划30条")):
        lines.append(table_row(label, value["components"][group]))
    for group, item in value["completion"].items():
        lines.append(table_row("端到端：" + group, item))
    lines.append(table_row("完整90会话汇总", value["completion_total"]))
    lines.append(table_row("另：默认定位兼容性审计90会话", value["default_location_audit"]))
    lines += ["", f"**定位策略口径：** 首轮保留当前默认定位，严格原标签通过{value['default_location_audit']['passed']}/90；"
        f"Trace证实其中{len(value['defaulted_ids'])}例使用配置默认位置，而冻结标签要求追问用户位置。这是实验前提冲突，不能全部归因模型错误。"
        "主表完整90会话为关闭默认位置后的独立整轮结果，不是从两轮挑选成功样本拼接；两轮消费和失败均保留，不宣称两轮差异是模型优化收益。"]
    lines += ["", "### 核心准确率与证据", "", "| 指标 | 分子/分母 | 实测 |", "|---|---|---|"]
    labels = {"domain_accuracy": "领域正确率", "intent_exact": "领域与动作联合正确率",
        "annotated_slot_accuracy": "已标注槽位字段准确率", "annotated_slots_exact": "整组标注槽位正确率",
        "clarification_field_accuracy": "澄清字段判定正确率", "necessary_clarification_recall": "必要澄清召回率",
        "necessary_clarification_correct_field_recall": "必要澄清正确字段召回率",
        "unnecessary_clarification_rate": "多余澄清率", "clarification_runtime_error": "澄清运行异常率",
        "complete_input_runtime_error": "完整信息对照运行异常率", "plan_exact": "目标／依赖／绑定整计划正确率"}
    for group, item in value["components"].items():
        for metric, score in item["metrics"].items():
            rate = f"{score['value']:.2%}" if score["value"] is not None else "N/A"
            lines.append(f"| {group}：{labels.get(metric,metric)} | {score['numerator']}/{score['denominator']} | {rate} |")
    total = value["completion_total"]
    lines.append(f"| 完整会话全部目标完成率 | {total['passed']}/{total['submitted']} | {total['passed']/total['submitted']:.2%} |")
    lines.append(f"| 目标级完成率 | {total['goals_completed']}/{total['goals_total']} | {total['goals_completed']/total['goals_total']:.2%} |")
    false = value["structured_false_completion"]
    rate = f"{false['numerator']/false['denominator']:.2%}" if false["denominator"] else "N/A"
    lines.append(f"| 结构化虚假完成代理指标 | {false['numerator']}/{false['denominator']} | {rate} |")
    no_inference = value["no_inference_constraints"]
    lines.append(f"| 明确不得推断的空值约束 | {no_inference['numerator']}/{no_inference['denominator']} | "
        f"{no_inference['numerator']/no_inference['denominator']:.2%} |")
    lines += ["", "### 测试条件与边界", "",
        "- 组件领域/动作与槽位没有喂标准领域；先跑当前Jev级联与DeepSeek回退，再调用领域理解及宿主规范化。数字代表当前理解流水线，不是裸模型能力。",
        "- 澄清15正/15负，组件及主端到端评测进程禁用配置默认定位以匹配冻结标签；组件图真实、搜索及写回执受控。首轮默认定位审计另列，不混成一个澄清率。",
        "- 规划30条强制送入当前全局规划器；检查领域、数量、等价依赖与绑定，不验已移到领域层的动作/槽位，不测rules门控是否漏规划。相对时间固定2026-10-27；理解和端到端按各manifest实际开始日。",
        "- 端到端单任务、多任务、澄清各30；真实ASGI handler、模型、Graph、MCP与存储。单并发，自动用户回复，210秒会话上限；未经过浏览器/TCP，不含真实人工思考时间。",
        "- 分母保留全部提交及失败，分位数包含失败/超时；缺失usage记未知，Token包括修复和失败尝试。单条失败可能同时产生多个验收错误，不能拆成多个独立分类样本。",
        "- 结构化虚假完成是‘状态完成但目标证据未通过’的代理指标，可能受oracle别名或标注限制影响，不等于已人工确认的自然语言虚假承诺率。工具选择、参数合法率仍缺独立全标签，不能由成功率替代。",
        "- 人工合成回归集，单次运行、无双人标注与未见留出保证；本轮不能证明线上准确率、峰值容量或P95 SLA。", "",
        "### 失败保留与排查入口", "", "| 专项 | 失败样本ID | 最低可复核证据 |", "|---|---|---|"]
    for row in value["component_failures"]:
        reason = row.get("error_type") or ", ".join(row.get("grade", {}).get("errors", [])) or "输出与冻结字段/澄清标签不一致"
        lines.append(f"| {row['group']} | {row['id']} | {reason.replace('|','/')} |")
    for row in value["completion_failures"]:
        lines.append(f"| {row['group']} | {row['id']} | {'; '.join(row['failures']).replace('|','/')} |")
    lines += ["", f"[本轮逐条结果、混淆矩阵、运行条件与用量]({PUBLIC.as_posix()}/README.md)。完整结果中保留prediction和失败参数；原始Trace与工具账本在隔离run数据库，可继续按任务排查。", ""]
    lines += ["### 从证据得到的优化优先级（尚未实施）", "",
        "1. **先修绑定契约和修复反馈。** 专项7个携带结果绑定的计划均在结构校验阶段失败；端到端天气保存Trace可看到非法左侧字段`result`。全局规划不输出工具参数，却需要知道绑定目标允许集合，应在契约中明确`details`等合法目标、提供真实来源路径示例，并把底层校验字段传给一次受限修复。不是增加盲目重试。专项没有保留完整原始异常输出，不能断言7条每一条的非法字段完全相同。",
        "2. **同领域也按独立动作拆目标。** ‘查看我的提醒和行程’被规划成一个planning目标，领域节点只能选择一个动作；Trace中确实只创建两项目标而不是三项。应明确每个独立可验收目标单列，不以领域数替代任务数，同时保留全局不抽业务槽位的边界。",
        "3. **先对齐评测前提，再谈准确率。** 默认位置是配置回退不是GPS；有位置与无位置的澄清标签分开维护。两轮原始失败全部保留，不事后改标签报提升。",
        "4. 修复时新建运行版本，再复验同一整组及额外未见留出集；本轮没有改变生产逻辑，不能声称这些建议已提高准确率或降低耗时。", ""]
    spend = value["experiment_consumption"]
    token_total = (str(spend["total_tokens"]) if spend["total_tokens"] is not None
        else f"至少{spend['observed_total_tokens']}（{spend['attempts_missing_usage']}次用量未知）")
    lines += ["### 全部本轮实验消耗", "", f"包含120条组件、默认定位90会话及无默认定位90会话的全部尝试："
        f"{spend['model_attempts']}次模型attempt，Token {token_total}。这是三组基准运行Trace内的消费汇总，不包含未归档的其他进程流量；"
        "不将复用场景当300个独立用户，也不计算混合质量/时延均值。", ""]
    if value.get("engineering_runs"):
        local, isolated = (value["engineering_runs"][key] for key in ("local_config", "isolated"))
        lines += ["### 本轮工程回归", "", "| 条件 | 通过 | 失败 | 跳过 |", "|---|---|---|---|",
            f"| 直接读取本地私有配置 | {local['passed']} | {local['failed']} | {local['skipped']} |",
            f"| 不带私有.env的隔离源码副本 | {isolated['passed']} | {isolated['failed']} | {isolated['skipped']} |", "",
            "本地配置下16个旧夹具未替换新增的事件理解入口，读到llm_direct后在事件Schema阶段失败；隔离副本使用受控测试默认入口，817通过、2跳过。"
            "没有因此切换正常服务为旧策略；当前llm_direct的真实效果以上述端到端另验。此处揭示测试环境耦合问题，不能把隔离通过解释为16个业务缺陷已经修复，"
            "也不能把817个工程用例当817个真实会话。[两轮摘要与失败ID](测试数据集/结果记录/2026-10-30-01/工程回归摘要.json)。", ""]
    return "\n".join(lines)
